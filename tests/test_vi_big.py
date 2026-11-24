"""Edit a 20 KiB exFAT document and verify its entire contents on the host."""
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from ref_exfat import Exfat
from test_accounts import boot, inputs, LOGIN, ROOT_PROMPT
from test_foundation import wait_for
from tools import gen_diskimg

NAME = "BIGDOC.C"
END = "ZQEND9Z"
INSERT = "qqtail7qq"
ROWS = [f"row{i:03} " + "abcdefghij" * 9 + f" tail{i:03}" for i in range(199)]
ROWS.append(END.ljust(105, "x"))
ORIGINAL = ("\n".join(ROWS) + "\n").encode()


def guest(qmp, serial, work):
    wait_for(serial, 0, lambda s: LOGIN in s)
    inputs(qmp, serial, "root", ROOT_PROMPT)
    inputs(qmp, serial, "cd /", "[root:/] > ")
    inputs(qmp, serial, "wc " + NAME, "\n200 ")
    inputs(qmp, serial, "tail " + NAME, END)
    inputs(qmp, serial, "vi " + NAME, "-- NORMAL --")
    start = serial.size()
    qmp.hmc("sendkey shift-g")
    wait_for(serial, start, lambda s: "200," in s)
    # Check what the user sees after G, independently of serial output.
    screen = work / "vga.bin"
    reply = qmp.hmc(f'pmemsave 0xb8000 4000 "{screen.as_posix()}"')
    assert not reply.get("return") and END.encode() in screen.read_bytes()[::2]
    for key in ("o", *INSERT, "esc"):
        qmp.hmc("sendkey " + key)
        time.sleep(0.15)
    inputs(qmp, serial, ":wq", "[root:/] > ")
    inputs(qmp, serial, "ver", "Cerulia Kernel version ")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="cerulia-vi-big-") as tmp:
        work = Path(tmp)
        disk = work / "data.vhd"
        with patch.object(gen_diskimg, "exfat_files", return_value=[(NAME, ORIGINAL)]):
            gen_diskimg.gen_exfat(disk)
        boot(disk, lambda q, s: guest(q, s, work), False)
        assert Exfat(disk).read_path(NAME) == ORIGINAL + INSERT.encode() + b"\n"
    print("PASS vi: all 200 lines read, last line visible and complete file saved")
