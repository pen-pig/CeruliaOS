"""Drive the default sign-in via QEMU's keyboard monitor and capture COM1."""
from pathlib import Path
import subprocess
import sys
import time


def main(qemu, image, disk, serial, timeout):
    log = Path(serial)
    log.write_bytes(b"")
    deadline = time.monotonic() + float(timeout)
    with subprocess.Popen([
        qemu, "-machine", "pc", "-vga", "std", "-m", "128", "-display", "none",
        "-no-reboot", "-monitor", "stdio", "-serial", f"file:{log}",
        "-drive", f"format=raw,file={image},snapshot=on",
        "-drive", f"format=raw,file={disk},snapshot=on",
    ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL) as vm:
        def wait(marker, offset=0):
            while time.monotonic() < deadline and vm.poll() is None:
                if marker in log.read_bytes()[offset:]:
                    return
                time.sleep(0.1)
            raise RuntimeError(f"guest did not reach {marker!r} (QEMU rc={vm.poll()})")

        def command(text):
            vm.stdin.write((text + "\n").encode("ascii"))
            vm.stdin.flush()

        def enter(line):
            for key in [*line, "ret"]:
                command(f"sendkey {key} 30")
                time.sleep(0.1)

        try:
            wait(b"Login: ")
            enter("user")
            prompt = b"[user:/home/user] > "
            wait(prompt)
            start = log.stat().st_size
            enter("whoami")
            wait(b"\nuser\r\n" + prompt, start)
            command("quit")
            if vm.wait(timeout=5) != 0:
                raise RuntimeError(f"QEMU exited with {vm.returncode}")
        finally:
            if vm.poll() is None:
                vm.kill()


if __name__ == "__main__":
    main(*sys.argv[1:])
