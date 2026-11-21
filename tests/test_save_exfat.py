"""An exFAT overwrite with insufficient free space must preserve the old file."""
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ezos_env import qemu_exe  # noqa: E402
from ezos_qemu import alloc_port, image_path  # noqa: E402
from test_nvme import Qmp, SerialReader, run_cmd  # noqa: E402
from test_accounts import LOGIN, inputs  # noqa: E402
from test_foundation import wait_for  # noqa: E402
from tools import gen_diskimg  # noqa: E402


def make_disk(disk):
    # Four metadata clusters, 95 data clusters and one free cluster. The last
    # file's primary entry and stream entry straddle two directory sectors.
    files = [("LONG_SOURCE_DATA.TXT", b"x" * 700),
             ("LONG_BITMAP_FILL.TXT", b"guard" + bytes(91 * 512 - 5)),
             ("MARK.TXT", b"mark"), ("KEEP.TXT", b"original-content")]
    with patch.object(gen_diskimg, "exfat_files", return_value=files):
        gen_diskimg.gen_exfat(disk)

    # Give the large file a real contiguous allocation: the bitmap alone owns
    # these clusters. Treating a zero FAT entry as free would corrupt the file.
    image = bytearray(disk.read_bytes())
    fat = 26 * 512
    image[fat + 8 * 4:fat + 99 * 4] = bytes(91 * 4)
    filler = 27 * 512 + 256
    image[filler + 33] = 3   # AllocationPossible | NoFatChain
    rewrite_checksum(image, filler)
    disk.write_bytes(image)


def checksum(image, offset):
    value = 0
    length = (image[offset + 1] + 1) * 32
    for i, byte in enumerate(image[offset:offset + length]):
        if i not in (2, 3):
            value = (gen_diskimg.uint16_ror1(value) + byte) & 0xffff
    return value


def rewrite_checksum(image, offset):
    struct.pack_into("<H", image, offset + 2, checksum(image, offset))


def login_for_test(qmp, serial, start):
    wait_for(serial, start, lambda s: LOGIN in s)
    inputs(qmp, serial, "root", "[root:")
    inputs(qmp, serial, "cd /", "[root:/] > ")


def main():
    qmp_port, serial_port = alloc_port(2)
    with tempfile.TemporaryDirectory(prefix="cerulia-save-") as work:
        disk = Path(work) / "disk.vhd"
        make_disk(disk)
        with subprocess.Popen([
            qemu_exe(), "-machine", "pc", "-m", "128", "-display", "none", "-nic", "none",
            "-drive", f"format=raw,file={image_path()},snapshot=on",
            "-drive", f"format=raw,file={disk},cache=writethrough",
            "-serial", f"tcp:127.0.0.1:{serial_port},server,wait",
            "-qmp", f"tcp:127.0.0.1:{qmp_port},server,nowait",
        ], cwd=ROOT, stdout=subprocess.DEVNULL) as vm:
            try:
                serial = SerialReader(serial_port)
                qmp = Qmp(qmp_port)
                login_for_test(qmp, serial, 0)

                def run(line, expected):
                    ok, output = run_cmd(qmp, serial, line, expected.lower())
                    assert ok, f"{line!r}: {output}\n{serial.snapshot()[-3000:]}"

                run("cp LONG_SOURCE_DATA.TXT KEEP.TXT", "Copy failed.")
                run("cat KEEP.TXT", "original-content")
                run("write keep.txt saved-content", "File written successfully.")
                run("cat KEEP.TXT", "saved-content")
                run("fsck", "0 problem(s) -- consistent")

                start = serial.size()
                qmp.type_line("reboot")
                login_for_test(qmp, serial, start)
                run("cat KEEP.TXT", "saved-content")
                qmp.quit()
                vm.wait(timeout=5)
            finally:
                if vm.poll() is None:
                    vm.kill()

        image = disk.read_bytes()
        entry = 27 * 512 + 480
        assert struct.unpack_from("<H", image, entry + 2)[0] == checksum(image, entry)
        first = struct.unpack_from("<I", image, entry + 32 + 20)[0]
        assert first == 101, first
        assert image[33 * 512:33 * 512 + 5] == b"guard"
        print("PASS exFAT failed save preserves original, successful save survives reboot")


if __name__ == "__main__":
    main()
