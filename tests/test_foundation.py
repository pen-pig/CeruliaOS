"""QEMU regressions for safe transfers, descriptor write-back and user faults."""
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ezos_env import qemu_exe, tool  # noqa: E402
from ezos_qemu import alloc_port, image_path  # noqa: E402
from test_nvme import Qmp, SerialReader  # noqa: E402
from tools import gen_diskimg as img  # noqa: E402


LIMIT = bytes(range(256)) * 256
BIG = LIMIT + b"!"
PROMPT = re.compile(r"\n\[[^\n\]]*\] > ")


def make_exfat(disk):
    files = [("TARGET.TXT", b"volume-one-original")]
    for num, name in enumerate(("CWD.ELF", "DISK.ELF", "UD.ELF", "PF.ELF"), 1):
        binary = disk.parent / name
        subprocess.run([tool("nasm"), "-f", "bin", f"-DCASE={num}", "-o", str(binary),
                        str(Path(__file__).with_name("foundation_probe.asm"))], check=True)
        files.append((name, binary.read_bytes()))
    with patch.object(img, "exfat_files", return_value=files):
        img.gen_exfat(disk)


def fat_layout():
    params = img.build_fat_params(32768 - 1, 4, 16)
    fat = img.PART_START + params["reserved"]
    root = fat + params["nfats"] * params["fat_size"]
    data = img.PART_START + params["first_data"]
    return params, fat, root * 512, data * 512


def make_fat16(disk):
    img.gen_fat(disk, 16)
    params, fat, root, data = fat_layout()
    volume = bytearray(disk.read_bytes())
    entries = [(b"TARGET  TXT", b"volume-two-original", None),
               (b"BIG     BIN", BIG, None), (b"LIMIT   BIN", LIMIT, None),
               (b"SHORT   BIN", b"broken chain", 4096)]
    cluster = 4  # skip README and the LFN sample
    off = root
    while volume[off]:
        off += 32
    for name, payload, declared in entries:
        first = cluster
        chunks = range(0, len(payload), 2048)
        for pos in chunks:
            start = data + (cluster - 2) * 2048
            chunk = payload[pos:pos + 2048]
            volume[start:start + len(chunk)] = chunk
            next_cluster = 0xffff if pos == chunks[-1] else cluster + 1
            for copy in range(params["nfats"]):
                struct.pack_into("<H", volume, (fat + copy * params["fat_size"]) * 512
                                 + cluster * 2, next_cluster)
            cluster += 1
        volume[off:off + 32] = img.make_dirent(name, 0x20, first, declared or len(payload))
        off += 32
    disk.write_bytes(volume)


def check_fat16(disk):
    volume = disk.read_bytes()
    _, fat, root, data = fat_layout()
    files = {}
    for off in range(root, data, 32):
        entry = volume[off:off + 32]
        if not entry[0]:
            break
        if entry[0] != 0xe5 and entry[11] == 0x20:
            files[entry[:11]] = struct.unpack_from("<HI", entry, 26)

    for name, expected in {b"BIG     BIN": BIG, b"TARGET  TXT": b"volume-two-original",
                           b"LIMIT   BIN": LIMIT, b"MOVED   BIN": LIMIT,
                           b"EMPTY2  TXT": b""}.items():
        cluster, size = files[name]
        assert size == len(expected), (name, size)
        content = bytearray()
        while len(content) < size:
            assert 2 <= cluster < 0xfff8, name
            start = data + (cluster - 2) * 2048
            content.extend(volume[start:start + min(2048, size - len(content))])
            cluster, = struct.unpack_from("<H", volume, fat * 512 + cluster * 2)
        assert content == expected, name
    assert b"COPY    BIN" not in files and b"LOST    BIN" not in files
    assert b"SHORT   BIN" in files and b"EMPTY   TXT" not in files


def wait_for(serial, start, predicate):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        output = serial.tail_from(start)
        if predicate(output):
            return output
        if "System halted" in output or "probe failure" in output:
            break
        time.sleep(0.05)
    raise AssertionError(f"unexpected guest output:\n{serial.tail_from(start)[-3200:]}")


def guest(qmp, serial):
    wait_for(serial, 0, lambda s: "\nLogin: " in s)
    qmp.type_line("root")
    wait_for(serial, 0, lambda s: "[root:/home/root] > " in s)

    def run(line, *expected):
        start = serial.size()
        qmp.type_line(line)
        # Background stdout can interrupt the prompt in the serial stream.
        return wait_for(serial, start, lambda s: "] > " in s.partition("\n")[2] and
                        all(marker in s for marker in expected))

    run("cd /", "[root:/] > ")
    run("mkdir ONE")
    run("mkdir TWO")
    run("write /ONE/TARGET.TXT old-one", "File written successfully.")
    run("write /TWO/TARGET.TXT old-two", "File written successfully.")
    run("cd ONE", "[root:/ONE] > ")
    run("/CWD.ELF &", "fd buffer staged")
    run("cd /TWO", "[root:/TWO] > ")
    run("touch /CWD.OK", "fd close committed")  # even an empty gate must open
    run("cat /ONE/TARGET.TXT", "committed-data")
    run("cat /TWO/TARGET.TXT", "old-two")

    run("cd /", "[root:/] > ")
    for _ in range(8):  # more than the available user slots if any leak
        run("exec UD.ELF", "fd buffer staged", "User process fault: #UD", "exited with code 134")
    run("exec PF.ELF", "User process fault: #PF", "exited with code 142")
    run("cat TARGET.TXT", "volume-one-original")
    run("mv TARGET.TXT ./target.txt", "Destination already exists.")
    run("cat TARGET.TXT", "volume-one-original")

    run("DISK.ELF &", "fd buffer staged")
    run("setdrive 2", "FS drive set to 2 (FAT16)")
    run("touch /DISK.OK", "fd close rejected stale mount")
    run("cat TARGET.TXT", "volume-two-original")
    run("cp BIG.BIN TARGET.TXT", "Source exceeds the 64 KiB transfer limit.")
    run("mv BIG.BIN LOST.BIN", "Source exceeds the 64 KiB transfer limit.")
    run("mv SHORT.BIN LOST.BIN", "Could not read the complete source file.")
    run("cp SHORT.BIN LOST.BIN", "Could not read the complete source file.")
    run("mv LIMIT.BIN ./limit.bin", "Destination already exists.")
    run("mv LIMIT.BIN TARGET.TXT", "Destination already exists.")
    run("cp LIMIT.BIN TARGET.TXT", "Destination already exists.")
    run("cp LIMIT.BIN COPY.BIN", "Copied.")
    run("mv COPY.BIN MOVED.BIN", "Moved.")
    run("touch EMPTY.TXT", "File created.")
    run("mv EMPTY.TXT EMPTY2.TXT", "Moved.")
    run("setdrive 1", "FS drive set to 1 (exFAT)")
    run("cat TARGET.TXT", "volume-one-original")
    # Reformat only the disposable fixture, without changing the drive number.
    run("DISK.ELF &", "fd buffer staged")
    run("format fat16", "Disk formatted as FAT16.")
    run("write TARGET.TXT new-volume-content", "File written successfully.")
    run("touch /DISK.OK", "fd close rejected stale mount")
    run("cat TARGET.TXT", "new-volume-content")

    start = serial.size()
    qmp.type_line("crash ud")
    wait_for(serial, start, lambda s: "Cerulia kernel panic" in s and "System halted" in s)


def main():
    qmp_port, serial_port = alloc_port(2)
    with tempfile.TemporaryDirectory(prefix="cerulia-foundation-") as work:
        exfat, fat16 = Path(work) / "exfat.vhd", Path(work) / "fat16.vhd"
        make_exfat(exfat)
        make_fat16(fat16)
        with subprocess.Popen([
            qemu_exe(), "-machine", "pc", "-m", "128", "-display", "none", "-nic", "none",
            "-drive", f"format=raw,index=0,file={image_path()},snapshot=on",
            "-drive", f"format=raw,index=1,file={exfat},cache=writethrough",
            "-drive", f"format=raw,index=2,file={fat16},cache=writethrough",
            "-serial", f"tcp:127.0.0.1:{serial_port},server,wait",
            "-qmp", f"tcp:127.0.0.1:{qmp_port},server,nowait",
        ], cwd=ROOT, stdout=subprocess.DEVNULL) as vm:
            try:
                serial = SerialReader(serial_port)
                qmp = Qmp(qmp_port)
                guest(qmp, serial)
                qmp.quit()
                vm.wait(timeout=5)
            finally:
                if vm.poll() is None:
                    vm.kill()
        check_fat16(fat16)
    print("PASS full transfers and failure integrity, descriptor write-back, Ring 3 fault isolation")


if __name__ == "__main__":
    main()
