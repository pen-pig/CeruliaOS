"""Exercise shell writes against disk-full and short-chain FAT16 fixtures."""
from pathlib import Path
import struct
import tempfile

from ref_fat import Fat
from test_accounts import boot, LOGIN
from test_foundation import BIG, fat_layout, make_fat16, wait_for
from tools.gen_diskimg import make_dirent, make_mbr


def login(qmp, serial):
    wait_for(serial, 0, lambda s: LOGIN in s)
    qmp.type_line("root")
    wait_for(serial, 0, lambda s: "[root:" in s)

    def run(cmd, marker=None):
        start = serial.size()
        qmp.type_line(cmd)
        return wait_for(serial, start, lambda s: bool(
            "] > " in (s if cmd.startswith(":") else s.partition("\n")[2])
            and (not marker or marker in s)))

    run("cd /")
    return run


def normal(qmp, serial):
    run = login(qmp, serial)
    run("echo -n suffix >> BIG.BIN")
    run("echo no >> SHORT.BIN", "cannot read entire target")
    run("vi SHORT.BIN", "could not read entire file")
    run("vi BIG.BIN", "exceeds 64 KiB")
    run("vi LIMIT.BIN", "line exceeds 127 bytes")
    run("cat LIMIT.BIN > TARGET.TXT", "output too large")
    run("cat LIMIT.BIN | wc", "next command not run")
    run("echo -n | wc", "0 0 0 (pipe)")
    run("echo bad > /system/no.txt", "write failed")
    run("echo -n > EMPTY.TXT")
    run("cd /home/root > HERE.TXT")
    run("cd /")
    run("echo ok > OUT.TXT")
    run("whoami >> OUT.TXT")
    qmp.type_line("vi TARGET.TXT")
    wait_for(serial, 0, lambda s: "-- NORMAL --" in s)
    run(":wq")
    run("write " + "z" * 128 + " ignored", "filename too long")


def disk_full(qmp, serial):
    run = login(qmp, serial)
    run("write TARGET.TXT replacement", "Failed to write file.")
    run("echo replacement > TARGET.TXT", "write failed")
    run("cat TARGET.TXT", "\nvolume-two-original")
    qmp.type_line("vi TARGET.TXT")
    wait_for(serial, 0, lambda s: "-- NORMAL --" in s)
    start = serial.size()
    qmp.type_line(":wq")
    wait_for(serial, start, lambda s: "save failed" in s)
    run(":q!")


def sector_fixture(path, bps):
    # 250 logical sectors, two one-sector FAT12 tables, one root block.
    total = 250
    image = bytearray(512 + total * bps)
    image[:512] = make_mbr(1, total * bps // 512)
    bs = memoryview(image)[512:]
    bs[:3] = b"\xeb\x3c\x90"
    struct.pack_into("<HBHBHHB H", bs, 11, bps, 1, 1, 2, bps // 32, total, 0xf8, 1)
    bs[510:512] = b"\x55\xaa"
    for off in (bps, 2*bps):
        bs[off:off+5] = b"\xf8\xff\xff\xff\x0f"  # cluster 2: EOC
    root = 3*bps
    for off in range(0, 512, 32):
        bs[root + off] = 0xe5
    bs[root+512:root+544] = make_dirent(b"TARGET  TXT", 0x20, 2, 8)
    bs[4*bps:4*bps+8] = b"original"
    path.write_bytes(image)


def sector_guest(qmp, serial):
    run = login(qmp, serial)
    run("cat TARGET.TXT", "\noriginal")
    run("write TARGET.TXT updated", "File written successfully.")
    run("cat TARGET.TXT", "\nupdated")


def main():
    with tempfile.TemporaryDirectory(prefix="cerulia-redirect-") as scratch:
        disk = Path(scratch) / "data.vhd"
        make_fat16(disk)
        boot(disk, normal, False)
        with Fat(disk) as volume:
            for name, expected in (("BIG.BIN", BIG + b"suffix"), ("TARGET.TXT", b"volume-two-original"),
                                   ("OUT.TXT", b"ok\nroot\n"), ("EMPTY.TXT", b"")):
                entry = volume.lookup(name)
                assert entry and volume.read(entry) == expected, name
            assert volume.lookup("HERE.TXT")
            assert volume.lookup("SHORT.BIN")["size"] == 4096

        # No free clusters. A delete-before-replace implementation would free
        # one and report success, changing the only copy of TARGET.TXT.
        params, fat, _, _ = fat_layout()
        image = bytearray(disk.read_bytes())
        for copy in range(params["nfats"]):
            off = (fat + copy * params["fat_size"]) * 512
            for cluster in range(2, params["clusters"] + 2):
                at = off + 2 * cluster
                if image[at:at+2] == b"\0\0":
                    struct.pack_into("<H", image, at, 0xffff)
        disk.write_bytes(image)
        boot(disk, disk_full, False)
        with Fat(disk) as volume:
            assert volume.read(volume.lookup("TARGET.TXT")) == b"volume-two-original"
        for bps in (1024, 2048, 4096):
            sector_fixture(disk, bps)
            boot(disk, sector_guest, False)
            with Fat(disk) as volume:
                assert volume.read(volume.lookup("TARGET.TXT")) == b"updated", bps
    print("PASS redirects, vi failure handling, full FAT16 preservation and large-sector FAT12")


if __name__ == "__main__":
    main()
