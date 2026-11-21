"""QEMU: provision, switch, confine and persist local users on a disposable exFAT disk."""
import argparse
import hashlib
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ezos_env import qemu_exe, tool  # noqa: E402
from ezos_qemu import alloc_port, image_path  # noqa: E402
from test_foundation import PROMPT, wait_for  # noqa: E402
from test_nvme import Qmp, SerialReader  # noqa: E402
from tools import gen_diskimg  # noqa: E402

ROOT_PASS = "Root testing phrase 17"
ALICE_PASS = "Alice test phrase 19"
ALICE_NEW = "Alice new phrase 22"
BOB_PASS = "Bob testing phrase 58"
NEW = "New password (15-64 characters): "
CONFIRM = "Confirm password: "
PASSWORD = "\nPassword: "
LOGIN = "\nLogin: "
ROOT_PROMPT = "[root:/home/root] > "
USER_PROMPT = "[user:/home/user] > "
ALICE_PROMPT = "[alice:/home/alice] > "


def inputs(qmp, serial, text, *expected):
    start = serial.size()
    qmp.type_line(text)
    return wait_for(serial, start, lambda s: all(x in s for x in expected))


def sign_in(qmp, serial, name, password, expected):
    if password is None:
        return inputs(qmp, serial, name, expected)
    inputs(qmp, serial, name, PASSWORD)
    inputs(qmp, serial, password, expected)


def enroll(qmp, serial, command, password, *expected):
    inputs(qmp, serial, command, NEW)
    inputs(qmp, serial, password, CONFIRM)
    inputs(qmp, serial, password, *expected)


def guest(qmp, serial):
    fresh = wait_for(serial, 0, lambda s: LOGIN in s)
    assert "root [admin] [no password]" in fresh and "user [no password]" in fresh
    assert not PROMPT.search(fresh)
    sign_in(qmp, serial, "user", None, USER_PROMPT)
    inputs(qmp, serial, "admin user on", "Command unavailable", USER_PROMPT)
    inputs(qmp, serial, "logout", LOGIN)
    sign_in(qmp, serial, "root", None, ROOT_PROMPT)
    inputs(qmp, serial, "admin root off", "must remain admin", ROOT_PROMPT)
    enroll(qmp, serial, "passwd", ROOT_PASS, "Password changed.", ROOT_PROMPT)
    inputs(qmp, serial, "admin user on", "Admin granted.", ROOT_PROMPT)
    inputs(qmp, serial, "su user", USER_PROMPT)
    enroll(qmp, serial, "useradd alice", ALICE_PASS, "Account created.", USER_PROMPT)
    sign_in(qmp, serial, "su root", ROOT_PASS, ROOT_PROMPT)
    enroll(qmp, serial, "useradd bob", BOB_PASS, "Account created.", ROOT_PROMPT)
    inputs(qmp, serial, "write secret.txt root-only-data", "File written successfully.", ROOT_PROMPT)
    inputs(qmp, serial, "write TARGET.TXT waiting-target", "File written successfully.", ROOT_PROMPT)
    inputs(qmp, serial, "cp /PROBE.ELF /home/alice/probe.elf", "Copied.", ROOT_PROMPT)
    inputs(qmp, serial, "/WAIT.ELF &", "fd buffer staged", ROOT_PROMPT)
    inputs(qmp, serial, "logout", "Stop or wait for running programs", ROOT_PROMPT)
    inputs(qmp, serial, "touch /CWD.OK", "fd close committed", ROOT_PROMPT)
    inputs(qmp, serial, "logout", LOGIN)
    sign_in(qmp, serial, "alice", ALICE_PASS, ALICE_PROMPT)
    history = inputs(qmp, serial, "history", ALICE_PROMPT)
    assert "secret.txt" not in history and "useradd" not in history
    inputs(qmp, serial, "write myfile.txt alice-owned-data", "File written successfully.", ALICE_PROMPT)
    inputs(qmp, serial, "cat myfile.txt", "\nalice-owned-data", ALICE_PROMPT)
    inputs(qmp, serial, "mkdir nested", ALICE_PROMPT)
    inputs(qmp, serial, "cd nested", "[alice:/home/alice/nested] > ")
    inputs(qmp, serial, "cd ..", ALICE_PROMPT)
    denied = inputs(qmp, serial, "cat ./../root/secret.txt", "File not found or read error.", ALICE_PROMPT)
    assert "root-only-data" not in denied
    inputs(qmp, serial, "cat /etc/accounts.db", "File not found or read error.", ALICE_PROMPT)
    inputs(qmp, serial, "write /home/alice/../bob/made.txt bad", "Failed to write file.", ALICE_PROMPT)
    inputs(qmp, serial, "exec probe.elf", "account syscalls isolated", ALICE_PROMPT)
    inputs(qmp, serial, "readdisk 0", "Command unavailable", ALICE_PROMPT)
    inputs(qmp, serial, "useradd test", "Command unavailable", ALICE_PROMPT)
    inputs(qmp, serial, "su root", PASSWORD)
    inputs(qmp, serial, "definitely-wrong", "Login failed.", ALICE_PROMPT)
    inputs(qmp, serial, "passwd", "Current password: ")
    inputs(qmp, serial, ALICE_PASS, NEW)
    inputs(qmp, serial, ALICE_NEW, CONFIRM)
    inputs(qmp, serial, ALICE_NEW, "Password changed.", ALICE_PROMPT)
    inputs(qmp, serial, "logout", LOGIN)
    sign_in(qmp, serial, "bob", BOB_PASS, "[bob:/home/bob] > ")
    inputs(qmp, serial, "cat /home/alice/myfile.txt", "File not found or read error.", "[bob:/home/bob] > ")

    start = serial.size()
    qmp.cmd("system_reset")
    boot = wait_for(serial, start, lambda s: "Local sign-in" in s and LOGIN in s)
    assert not PROMPT.search(boot)
    sign_in(qmp, serial, "alice", ALICE_PASS, "Login failed.")
    wait_for(serial, start, lambda s: s.count(LOGIN) == 2)
    sign_in(qmp, serial, "alice", ALICE_NEW, ALICE_PROMPT)
    inputs(qmp, serial, "cat myfile.txt", "\nalice-owned-data", ALICE_PROMPT)
    inputs(qmp, serial, "su user", USER_PROMPT)
    inputs(qmp, serial, "users", "user [admin] [no password]", USER_PROMPT)
    inputs(qmp, serial, "cat /home/root/secret.txt", "root-only-data", USER_PROMPT)
    for password in (ROOT_PASS, ALICE_PASS, ALICE_NEW, BOB_PASS):
        assert password not in serial.snapshot(), "password echoed in guest output"
    assert "USB-KBD: key=" not in serial.snapshot().split(NEW, 1)[1], "USB keyboard logged private input"


def boot(disk, action, usb_keyboard):
    qmp_port, serial_port = alloc_port(2)
    hardware = ["-machine", "pc,i8042=off", "-device", "qemu-xhci",
                "-device", "usb-kbd"] if usb_keyboard else ["-machine", "pc"]
    with subprocess.Popen([
        qemu_exe(), *hardware, "-m", "128", "-display", "none", "-nic", "none",
        "-drive", f"format=raw,index=0,file={image_path()},snapshot=on",
        "-drive", f"format=raw,index=1,file={disk},cache=writethrough",
        "-serial", f"tcp:127.0.0.1:{serial_port},server,wait",
        "-qmp", f"tcp:127.0.0.1:{qmp_port},server,nowait",
    ], cwd=ROOT, stdout=subprocess.DEVNULL) as vm:
        serial = SerialReader(serial_port)
        try:
            qmp = Qmp(qmp_port)
            action(qmp, serial)
            qmp.quit()
            vm.wait(timeout=5)
        finally:
            serial.close()
            if vm.poll() is None:
                vm.kill()


def corrupt_after_verifying_hashes(disk):
    image = bytearray(disk.read_bytes())
    header = struct.pack("<4sII", b"CCA2", 4, 4)
    offset = image.index(header)
    end = offset + 12 + 16 * 66
    # The small exFAT fixture allocates the multi-cluster replacement contiguously.
    assert hashlib.sha256(image[offset:end]).digest() == image[end:end + 32]
    salts = set()
    rows = (("root", ROOT_PASS, 3), ("user", None, 1), ("alice", ALICE_NEW, 2), ("bob", BOB_PASS, 2))
    for i, (name, password, flags) in enumerate(rows):
        entry = offset + 12 + i * 66
        assert image[entry:entry + 17].rstrip(b"\0") == name.encode()
        assert image[entry + 65] == flags
        if password is None:
            assert not any(image[entry + 17:entry + 65])
            continue
        salt = bytes(image[entry + 17:entry + 33])
        assert salt not in salts
        salts.add(salt)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000)
        assert image[entry + 33:entry + 65] == digest
        assert password.encode() not in image
    image[offset + 45] ^= 1
    disk.write_bytes(image)


def broken_database(qmp, serial):
    output = wait_for(serial, 0, lambda s: "Account database is missing or damaged." in s)
    assert not PROMPT.search(output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--usb-keyboard", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="cerulia-accounts-") as tmp:
        work = Path(tmp)
        disk, probe, waiter = work / "accounts.vhd", work / "probe.elf", work / "wait.elf"
        test_sources = Path(__file__).parent
        subprocess.run([tool("nasm"), "-f", "bin", "-o", str(probe),
                        str(test_sources / "accounts_probe.asm")], check=True)
        subprocess.run([tool("nasm"), "-f", "bin", "-DCASE=1", "-o", str(waiter),
                        str(test_sources / "foundation_probe.asm")], check=True)
        with patch.object(gen_diskimg, "exfat_files", return_value=[
            ("PROBE.ELF", probe.read_bytes()), ("WAIT.ELF", waiter.read_bytes())
        ]):
            gen_diskimg.gen_exfat(disk)
        boot(disk, guest, args.usb_keyboard)
        before = hashlib.sha256(disk.read_bytes()).digest()
        subprocess.run([sys.executable, str(ROOT / "tools" / "gen_diskimg.py"),
                        str(disk), "--if-missing"], check=True)
        assert hashlib.sha256(disk.read_bytes()).digest() == before
        corrupt_after_verifying_hashes(disk)
        boot(disk, broken_database, args.usb_keyboard)
    print("PASS local sign-in, shell/syscall isolation, session guards, password persistence and corruption recovery")


if __name__ == "__main__":
    main()
