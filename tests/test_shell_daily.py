"""Check a standard account's everyday commands on a disposable data disk."""
from pathlib import Path
import tempfile

from test_accounts import boot, inputs, LOGIN, ROOT_PROMPT, USER_PROMPT
from test_foundation import wait_for
from tools import gen_diskimg


def guest(qmp, serial):
    wait_for(serial, 0, lambda s: LOGIN in s)
    inputs(qmp, serial, "user", USER_PROMPT)
    inputs(qmp, serial, "write note.txt remember", "File written successfully.")
    inputs(qmp, serial, "touch note.txt", "\n", USER_PROMPT)
    inputs(qmp, serial, "cat note.txt", "\nremember\r\n")
    inputs(qmp, serial, "touch empty.txt", "File created.")
    inputs(qmp, serial, "touch empty.txt", "File already exists; contents unchanged.")
    inputs(qmp, serial, "touch .", "Failed to create file.")
    inputs(qmp, serial, "ver", "Cerulia Kernel version ")
    inputs(qmp, serial, "format exfat", "Command unavailable")
    inputs(qmp, serial, "history -c", "History cleared.")
    for n in range(10):
        inputs(qmp, serial, f"echo item{n}", f"\nitem{n}\r\n")
    h = inputs(qmp, serial, "history", "8: history")
    assert "1: echo item3" in h and "7: echo item9" in h and "echo item2" not in h
    inputs(qmp, serial, "su root", ROOT_PROMPT)
    h = inputs(qmp, serial, "history", "1: history")
    assert "item9" not in h


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="cerulia-daily-shell-") as scratch:
        disk = Path(scratch) / "data.vhd"
        gen_diskimg.gen_exfat(disk)
        boot(disk, guest, False)
    print("PASS standard shell: touch preserves data, ver, history rollover and session reset")
