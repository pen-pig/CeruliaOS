# -*- coding: utf-8 -*-
"""test_ext4mkfs.py - 反向互操作：mkfs.ext4 造的 metadata_csum 卷，内核必须能挂载、
读写、删除，且写完之后 Linux e2fsck -fn 仍然判卷干净。

正向（内核 format 卷被 e2fsck 接受）由 test_ext4vol + 手工 e2fsck 覆盖；这里钉死
反向：内核要能**吃下** Linux 工具造的真卷——64bit + desc_size=64 + 256B inode，
以及 fail closed（手工破坏一个组描述符校验和后，内核必须拒绝挂载）。

依赖 WSL（mkfs.ext4 / e2fsck），没有 WSL 时跳过而不是失败。
端口 4605(QMP) / 4606(serial)。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
from ezos_qemu import alloc_port, image_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402
from test_nvme import SerialReader, Qmp, wait_for, wait_port_free, kill_all_qemu  # noqa: E402
from test_ext4vol import rec  # noqa: E402

QMP_PORT, SER_PORT = alloc_port(2)
IMG = image_path()
QEMU = qemu_exe()

VOL_BYTES = 16 * 1024 * 1024 - 512          # 分区大小（留 512B 给 MBR）
PART = os.path.join(HERE, "mkfs_part.img").replace("\\", "/")
FULL = os.path.join(HERE, "mkfs_full.img").replace("\\", "/")


def wsl(cmd):
    # Windows 路径换成 /mnt/d/...：cmd 里的 D:\xxx 或 D:/xxx
    cmd = cmd.replace("D:\\", "/mnt/d/").replace("D:/", "/mnt/d/")
    return subprocess.run(["wsl", "-e", "bash", "-c", cmd],
                          capture_output=True, text=True, timeout=120)


def flat(s):
    return " ".join(s.split())


def boot_and_run(disk_img, script):
    """把 disk_img 挂为第二块盘启动内核，依次执行 script 里的 shell 命令，
    返回每条命令的串口输出。"""
    proc = subprocess.Popen(
        [QEMU, "-icount", "shift=auto", "-vga", "std",
         "-drive", "format=raw,file=" + IMG,
         "-drive", "format=raw,file=" + disk_img,
         "-qmp", "tcp:127.0.0.1:%d,server,nowait" % QMP_PORT,
         "-serial", "tcp:127.0.0.1:%d,server,wait" % SER_PORT],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    serial = SerialReader(SER_PORT)
    qmp = Qmp(QMP_PORT)
    outs = []
    try:
        wait_for(serial, "TASK: preemptive", 150)
        wait_for(serial, "[/] >", 120)
        time.sleep(1.0)
        for cmd, settle in script:
            before = serial.size()
            qmp.type_line(cmd)
            t0 = time.time()
            last, stable = -1, 0.0
            while time.time() - t0 < 180:
                time.sleep(0.3)
                if time.time() - t0 < settle:
                    continue
                n = serial.size()
                if n == last:
                    stable += 0.3
                    if stable >= 1.5:
                        break
                else:
                    stable, last = 0.0, n
            outs.append(flat(serial.tail_from(before)))
    finally:
        try:
            qmp.quit()
        except Exception:
            pass
        try:
            serial.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=15)
        except Exception:
            kill_all_qemu()
        wait_port_free(QMP_PORT, 20)
    return outs


def main():
    if not os.path.isfile(IMG):
        print("MISSING os-image.bin - run ninja first")
        return 2
    if wsl("true").returncode != 0:
        print("SKIP: WSL unavailable")
        return 0
    results = []

    # 1) mkfs.ext4 造分区镜像（1KB 块 + metadata_csum，默认 256B inode/64bit）
    if os.path.isfile(PART):
        os.remove(PART)
    r = wsl("truncate -s %d %s && mkfs.ext4 -q -F -b 1024 -O metadata_csum %s"
            % (VOL_BYTES, PART, PART))
    rec(results, "mkfs.ext4 creates metadata_csum volume",
        r.returncode == 0, "| %s" % (r.stderr[-70:] if r.returncode else ""))

    # 2) 拼 MBR：分区表项 type 0x83 / LBA 1 / 32766 扇区
    with open(FULL, "wb") as f:
        mbr = bytearray(512)
        mbr[446 + 4] = 0x83
        mbr[446 + 8:446 + 12] = (1).to_bytes(4, "little")
        mbr[446 + 12:446 + 16] = (VOL_BYTES // 512).to_bytes(4, "little")
        mbr[510:512] = b"\x55\xaa"
        f.write(mbr)
        with open(PART, "rb") as p:
            while True:
                chunk = p.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)

    # 3) 内核挂载 mkfs 卷：ls 应列出 lost+found，写/读/删都走通
    script = [("ls", 2.5), ("write HELLO.TXT hello-from-kernel", 2.0),
              ("cat HELLO.TXT", 2.0), ("write TMP.TXT bye", 1.5),
              ("rm TMP.TXT", 1.5)]
    outs = boot_and_run(FULL, script)
    rec(results, "kernel mounts mkfs volume (ls shows lost+found)",
        "lost+found" in outs[0].lower(), "| %s" % outs[0][-70:])
    rec(results, "kernel writes file to mkfs volume",
        "hello-from-kernel" in outs[2], "| %s" % outs[2][-70:])
    rec(results, "kernel removes file on mkfs volume",
        "error" not in outs[4].lower(), "| %s" % outs[4][-70:])

    # 4) e2fsck -fn 判卷干净：内核写路径维护的校验和必须全部正确。
    #    FULL 带 MBR，e2fsck 拒绝直接检查整盘镜像，提取分区部分再跑。
    with open(PART, "wb") as out_f, open(FULL, "rb") as in_f:
        in_f.seek(512)
        while True:
            chunk = in_f.read(1 << 20)
            if not chunk:
                break
            out_f.write(chunk)
    r = wsl("e2fsck -fn %s" % PART)
    out = (r.stdout or "") + (r.stderr or "")
    # e2fsck 退出码：0=干净；中文 locale 没有 "clean" 字样，只看返回码
    clean = r.returncode == 0 and "修复" not in out and "rror" not in out
    rec(results, "e2fsck -fn clean after kernel writes", clean,
        "| rc=%d %s" % (r.returncode, flat(out[-90:])))
    if not clean:
        print("--- full e2fsck output ---")
        print(out)
        print("--- PART kept for debugging: %s" % PART)

    # 5) fail closed：破坏 group 0 描述符的 bg_checksum（GDT 在块 2 = 偏移 2048+512）
    with open(FULL, "r+b") as f:
        f.seek(512 + 2048 + 30)
        orig = f.read(2)
        f.seek(512 + 2048 + 30)
        f.write(bytes([orig[0] ^ 0xFF, orig[1]]))
    outs = boot_and_run(FULL, [("ls", 2.5)])
    rec(results, "kernel refuses mount with corrupted GD checksum",
        "lost+found" not in outs[0].lower() or "error" in outs[0].lower(),
        "| %s" % outs[0][-70:])

    if clean:
        os.remove(PART)

    bad = [n for n, ok in results if not ok]
    print("\n%d/%d passed" % (len(results) - len(bad), len(results)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
