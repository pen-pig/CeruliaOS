"""QEMU regression for effective admin permissions and ext4 mode/owner checks."""
import hashlib
from pathlib import Path
import struct
import tempfile
from unittest.mock import patch

from test_accounts import (
    ALICE_PASS, ALICE_PROMPT, LOGIN, ROOT_PASS, ROOT_PROMPT, USER_PROMPT, boot, inputs, sign_in,
)
from test_foundation import wait_for
from tools import gen_diskimg


def ext4_guest(qmp, serial):
    wait_for(serial, 0, lambda s: LOGIN in s)
    sign_in(qmp, serial, "root", None, ROOT_PROMPT)
    inputs(qmp, serial, "format ext4", "Disk formatted as ext4.", "[root:/] > ")
    inputs(qmp, serial, "mkdir /private", "[root:/] > ")
    inputs(qmp, serial, "write /private/secret.txt superuser-data", "File written successfully.", "[root:/] > ")
    inputs(qmp, serial, "chmod 000 /private/secret.txt", "mode changed", "[root:/] > ")
    inputs(qmp, serial, "su user", USER_PROMPT)
    inputs(qmp, serial, "write own.txt first", "File written successfully.", USER_PROMPT)
    inputs(qmp, serial, "ls -l", "-rw-r--r-- user", "own.txt", USER_PROMPT)
    inputs(qmp, serial, "chmod 644 own.txt", "mode changed", USER_PROMPT)
    inputs(qmp, serial, "write own.txt second", "File written successfully.", USER_PROMPT)
    inputs(qmp, serial, "ls -l", "-rw-r--r-- user", "own.txt", USER_PROMPT)
    inputs(qmp, serial, "chmod 444 own.txt", "mode changed", USER_PROMPT)
    denied = inputs(qmp, serial, "write own.txt should-not-change", USER_PROMPT)
    assert "File written successfully." not in denied
    inputs(qmp, serial, "cat own.txt", "\nsecond", USER_PROMPT)
    inputs(qmp, serial, "mkdir closed", USER_PROMPT)
    inputs(qmp, serial, "write closed/file.txt nested-data", "File written successfully.", USER_PROMPT)
    inputs(qmp, serial, "chmod 600 closed", "mode changed", USER_PROMPT)
    inputs(qmp, serial, "cd closed", "no such directory", USER_PROMPT)
    inputs(qmp, serial, "cat closed/file.txt", "File not found or read error.", USER_PROMPT)
    inputs(qmp, serial, "cp /bin/hello.elf own.elf", "Copied.", USER_PROMPT)
    inputs(qmp, serial, "exec own.elf", "execute permission denied", USER_PROMPT)
    inputs(qmp, serial, "chmod 755 own.elf", "mode changed", USER_PROMPT)
    inputs(qmp, serial, "exec own.elf", "exited with code 42", USER_PROMPT)
    inputs(qmp, serial, "cat ../root/secret.txt", "File not found or read error.", USER_PROMPT)
    inputs(qmp, serial, "admin user on", "Command unavailable", USER_PROMPT)
    inputs(qmp, serial, "su root", ROOT_PROMPT)
    inputs(qmp, serial, "admin user on", "for this boot only", ROOT_PROMPT)
    inputs(qmp, serial, "su user", USER_PROMPT)
    inputs(qmp, serial, "cat /private/secret.txt", "\nsuperuser-data", USER_PROMPT)
    inputs(qmp, serial, "chown user /private/secret.txt", "owner changed", USER_PROMPT)
    inputs(qmp, serial, "chmod 600 /private/secret.txt", "mode changed", USER_PROMPT)
    inputs(qmp, serial, "cd /private", "[user:/private] > ")
    inputs(qmp, serial, "admin user off", "for this boot only", USER_PROMPT)
    inputs(qmp, serial, "cat /private/secret.txt", "File not found or read error.", USER_PROMPT)


def old_database():
    rows = []
    for i, (name, password) in enumerate((("root", ROOT_PASS), ("alice", ALICE_PASS)), 1):
        salt = bytes([i]) * 16
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000)
        rows.append(struct.pack("<17s16s32s", name.encode(), salt, h))
    body = struct.pack("<4sII", b"CCA1", len(rows), len(rows)) + b"".join(rows).ljust(16 * 65, b"\0")
    return body + hashlib.sha256(body).digest()


def install_old(qmp, serial):
    wait_for(serial, 0, lambda s: LOGIN in s)
    sign_in(qmp, serial, "root", None, ROOT_PROMPT)
    inputs(qmp, serial, "mkdir /etc", ROOT_PROMPT)
    inputs(qmp, serial, "cp /OLD.DB /etc/accounts.db", "Copied.", ROOT_PROMPT)
    inputs(qmp, serial, "write /home/root/secret.txt legacy-data", "File written successfully.", ROOT_PROMPT)


def check_migration(qmp, serial):
    start = wait_for(serial, 0, lambda s: LOGIN in s)
    assert "root [admin]" in start and "alice" in start and "[no password]" not in start
    sign_in(qmp, serial, "root", ROOT_PASS, ROOT_PROMPT)
    inputs(qmp, serial, "admin alice on", "Admin granted.", ROOT_PROMPT)
    sign_in(qmp, serial, "su alice", ALICE_PASS, ALICE_PROMPT)
    inputs(qmp, serial, "cat /home/root/secret.txt", "legacy-data", ALICE_PROMPT)


def main():
    with tempfile.TemporaryDirectory(prefix="cerulia-admin-") as work:
        disk, legacy = Path(work) / "work.vhd", Path(work) / "old.vhd"
        with patch.object(gen_diskimg, "exfat_files", return_value=[]):
            gen_diskimg.gen_exfat(disk)
        boot(disk, ext4_guest, False)
        with patch.object(gen_diskimg, "exfat_files", return_value=[("OLD.DB", old_database())]):
            gen_diskimg.gen_exfat(legacy)
        boot(legacy, install_old, False)
        boot(legacy, check_migration, False)
        assert b"CCA2" in legacy.read_bytes()
    print("PASS ext4 ownership/modes, promotion/revocation and CCA1 account migration")


if __name__ == "__main__":
    main()
