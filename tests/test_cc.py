"""Edit, compile and run C in Cerulia; verify the files survive a reboot."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ezos_env import qemu_exe  # noqa: E402
from ezos_qemu import alloc_port, disk_path, image_path  # noqa: E402
from test_nvme import KEYMAP, Qmp, SerialReader  # noqa: E402


def wait(serial, start, marker):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        out = serial.tail_from(start)
        if marker in out:
            return out
        time.sleep(0.1)
    raise AssertionError(f"missing {marker!r}:\n{serial.tail_from(start)[-3200:]}")


def type_keys(qmp, s):
    keys = {**KEYMAP, "{": "shift-bracket_left", "}": "shift-bracket_right",
            "\n": "ret"}
    for ch in s:
        key = keys.get(ch, "shift-" + ch.lower() if ch.isupper() else ch)
        qmp.hmc("sendkey " + key)
        time.sleep(0.05)


def command(qmp, serial, line):
    start = serial.size()
    qmp.type_line(line)
    return wait(serial, start, "\n[/] > ")


def main():
    qmp_port, serial_port = alloc_port(2)
    with tempfile.TemporaryDirectory(prefix="cerulia-cc-") as work:
        disk = Path(work) / "disk.vhd"
        shutil.copyfile(disk_path(), disk)
        with subprocess.Popen([
            qemu_exe(), "-machine", "pc", "-m", "128", "-display", "none", "-nic", "none",
            "-drive", f"format=raw,file={image_path()},snapshot=on",
            "-drive", f"format=raw,file={disk}",
            "-serial", f"tcp:127.0.0.1:{serial_port},server,wait",
            "-qmp", f"tcp:127.0.0.1:{qmp_port},server,nowait",
        ], cwd=ROOT, stdout=subprocess.DEVNULL) as vm:
            try:
                serial = SerialReader(serial_port)
                qmp = Qmp(qmp_port)
                wait(serial, 0, "[/] > ")
                qmp.type_line("vi cc-demo.c")
                time.sleep(0.6)
                type_keys(qmp, 'iint sum(int x){int s=0;while(x>0){s=s+x;x=x-1;}return s;}\n'
                               'int main(void){printf("sum=%d\\n",sum(5));return 42;}')
                start = serial.size()
                qmp.hmc("sendkey esc")
                time.sleep(0.2)
                type_keys(qmp, ":wq\n")
                wait(serial, start, "[/] > ")

                out = command(qmp, serial, "exec cc cc-demo.c cc-demo.elf")
                assert "cc: saved cc-demo.elf" in out, out
                out = command(qmp, serial, "exec cc-demo.elf")
                assert "sum=15" in out and "exited with code 42" in out, out
                print("PASS guest edit, compile and native execution", flush=True)

                command(qmp, serial, "write cc-bad.c int main(){return 5;")
                out = command(qmp, serial, "exec cc cc-bad.c cc-demo.elf")
                assert "cc: error" in out and "exited with code 1" in out, out
                out = command(qmp, serial, "exec cc-demo.elf")
                assert "sum=15" in out and "exited with code 42" in out, out
                print("PASS syntax error preserves previous executable", flush=True)

                start = serial.size()
                qmp.type_line("reboot")
                wait(serial, start, "Cerulia Shell - Type")
                wait(serial, start, "\n[/] > ")
                out = command(qmp, serial, "cat cc-demo.c")
                assert "int sum(int x)" in out, out
                out = command(qmp, serial, "exec cc-demo.elf")
                assert "sum=15" in out and "exited with code 42" in out, out
                print("PASS source and executable survive reboot", flush=True)
            finally:
                if vm.poll() is None:
                    vm.kill()


if __name__ == "__main__":
    main()
