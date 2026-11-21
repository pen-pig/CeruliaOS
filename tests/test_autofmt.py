"""Boot must preserve unrecognized media; only an explicit format enables saves."""
import hashlib
from pathlib import Path
import tempfile

from test_accounts import LOGIN, USER_PROMPT, boot, inputs
from test_foundation import wait_for

SIZE = 16 * 1024 * 1024
RESCUE = "[root:/system] > "


def rescue(qmp, serial):
    log = wait_for(serial, 0, lambda s: LOGIN in s)
    assert "NOT formatting" in log and "files cannot be saved" in log, log[-3000:]
    inputs(qmp, serial, "root", RESCUE)
    inputs(qmp, serial, "write /note.txt disposable", "Not saved: no data volume mounted.", RESCUE)
    inputs(qmp, serial, "ver", "Cerulia Kernel version", RESCUE)


def provision(qmp, serial):
    rescue(qmp, serial)
    inputs(qmp, serial, "format exfat", "Disk formatted as exFAT.")
    inputs(qmp, serial, "su user", USER_PROMPT)
    inputs(qmp, serial, "write note.txt first", "File written successfully.", USER_PROMPT)
    inputs(qmp, serial, "write note.txt kept", "File written successfully.", USER_PROMPT)


def cold_read(qmp, serial):
    wait_for(serial, 0, lambda s: LOGIN in s)
    inputs(qmp, serial, "user", USER_PROMPT)
    inputs(qmp, serial, "cat note.txt", "\nkept\r\n", USER_PROMPT)


def main():
    with tempfile.TemporaryDirectory(prefix="cerulia-boot-storage-") as scratch:
        disk = Path(scratch) / "data.raw"
        for kind in ("blank", "erased", "unknown-mbr", "blank-header"):
            data = bytearray(b"\xff" if kind == "erased" else b"\0") * SIZE
            if kind == "unknown-mbr":
                data[450] = 0x83
                data[454:458] = (2048).to_bytes(4, "little")
                data[458:462] = (SIZE // 512 - 2048).to_bytes(4, "little")
                data[510:512] = b"\x55\xaa"
            if kind in ("unknown-mbr", "blank-header"):
                data[SIZE // 2:SIZE // 2 + 4] = b"KEEP"
            disk.write_bytes(data)
            before = hashlib.sha256(data).digest()
            boot(disk, rescue, False)
            assert hashlib.sha256(disk.read_bytes()).digest() == before, kind
            print(f"PASS {kind}: rescue shell works; full disk unchanged", flush=True)

        # New disposable disk, not the unknown disk used above.
        disk = Path(scratch) / "fresh.raw"
        with disk.open("xb") as f:
            f.truncate(SIZE)
        boot(disk, provision, False)
        boot(disk, cold_read, False)
        print("PASS explicit exFAT format, user save/replace and cold-boot readback")


if __name__ == "__main__":
    main()
