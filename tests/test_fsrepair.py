# -*- coding: utf-8 -*-
"""test_fsrepair.py - 孤儿回收必须真的修，而不是只会喊

崩溃窗口：位图/分配表先持久、目录项最后写。崩在中间会留下"分配表说占用、
目录树够不到"的孤儿（ext4 是 inode，FAT/exFAT 是簇）。P1 给了两条修复通道：

  * FAT/exFAT：显式 `fsck -a` —— 普通 `fsck` 保持只读检测（test_fsck.py 的
    orphan 检测必须继续有效），`-a` 回收后复扫必须 consistent。
  * ext4：挂载即修复 —— 串口出现 repair note，文件从 ls 里消失，
    释放的空间能立刻被新文件用到。

损伤怎么造：和 test_fsck.py 同源的定向损伤（宿主机参考实现改一个字节），
可精确预测结果：FAT32 把空闲簇的 FAT 项改 EOC；exFAT 把位图空闲簇的 bit
置 1；ext4 把 HELLO.TXT 目录项的 inode 字段清零（目录块其余部分不动）。

ext4 修复后不跑 e2fsck 对拍：metadata_csum 卷的目录块校验和会被宿主机
改动弄失效，那是损伤制造方式的伪影而非修复缺陷；内核侧断言（repair
note + ls + 新文件写入）已经覆盖"孤儿真的被回收"。
端口由 ezos_qemu.alloc_port() 分配（每脚本唯一，支持 -j 并行）。
用法：python tests/test_fsrepair.py   （退出码 0 = 全通过）
"""
import os as _os_ez
import sys as _sys_ez
_sys_ez.path.insert(0, _os_ez.path.dirname(_os_ez.path.abspath(__file__)))
from ezos_qemu import (alloc_port, image_path, disk_path,
                       log_path, kill_stale_qemu)
import os as _os_ezos
import sys as _sys_ezos
_sys_ezos.path.append(_os_ezos.path.dirname(
    _os_ezos.path.dirname(_os_ezos.path.abspath(__file__))))
from ezos_env import qemu_exe  # noqa: E402

import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

QEMU = qemu_exe()
PORT = alloc_port()
PROMPT = b"[/] >"
BOOT_TIMEOUT = 120.0
CMD_TIMEOUT = 90.0

IMG = image_path()
SRC = disk_path()
LOG = log_path(os.path.join(HERE, "fsrepair_serial.log").replace("\\", "/"))
DISK = os.path.join(HERE, "fsrepair_disk.img").replace("\\", "/")
DISK_BYTES = 64 * 1024 * 1024

KEYMAP = {' ': 'spc', '.': 'dot', '-': 'minus', '_': 'shift-minus',
          '/': 'slash', '*': 'kp_multiply', '+': 'kp_add'}

results = []


def rec(name, ok, note=""):
    results.append((name, ok, note))
    print("  [%s] %s%s" % ("OK" if ok else "FAIL", name,
                           ("  -- " + note) if note else ""))
    sys.stdout.flush()


class Qmp(object):
    def __init__(self, port):
        for _ in range(60):
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("QMP connect failed")
        self.sock.settimeout(20)
        self.f = self.sock.makefile("rwb")
        self._read()
        self.cmd("qmp_capabilities")

    def _read(self):
        try:
            line = self.f.readline()
        except socket.timeout:
            raise RuntimeError("QMP read timed out")
        if not line:
            raise RuntimeError("QMP connection closed")
        return json.loads(line) if line.strip() else None

    def cmd(self, name, **args):
        self.f.write((json.dumps({"execute": name, "arguments": args}) + "\n").encode())
        self.f.flush()
        while True:
            r = self._read()
            if r is None:
                continue
            if "return" in r or "error" in r:
                return r

    def hmc(self, c):
        return self.cmd("human-monitor-command", **{"command-line": c})

    def type_line(self, s):
        for ch in s:
            key = KEYMAP.get(ch)
            if key is None:
                key = ('shift-' + ch.lower()) if ('A' <= ch <= 'Z') else ch.lower()
            r = self.hmc("sendkey " + key)
            if "error" in r:
                raise RuntimeError("sendkey %r failed: %r" % (key, r))
            time.sleep(0.05)
        self.hmc("sendkey ret")


def read_log():
    try:
        with open(LOG, "rb") as f:
            return f.read()
    except OSError:
        return b""


def flat(b):
    return " ".join(b.decode("latin1", "replace").lower().split())


def wait_for(needle, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if needle in read_log():
            return True
        time.sleep(0.5)
    return False


def run_cmd(qmp, cmd, timeout=CMD_TIMEOUT):
    n0 = len(read_log())
    qmp.type_line(cmd)
    end = time.time() + timeout
    while time.time() < end:
        new = read_log()[n0:]
        if b"\r\n" in new and new.rstrip().endswith(b">"):
            return True, flat(new)
        time.sleep(0.5)
    return False, flat(read_log()[n0:])


def port_in_use(port):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
        s.close()
        return True
    except OSError:
        return False


def wait_port_free(port, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not port_in_use(port):
            return True
        time.sleep(0.3)
    return False


def launch(disk):
    if port_in_use(PORT):
        kill_stale_qemu("qemu-system-x86_64.exe")
        wait_port_free(PORT, 15)
    if not wait_port_free(PORT, 10):
        raise RuntimeError("PORT %d occupied" % PORT)
    try:
        os.remove(LOG)
    except OSError:
        pass
    p = subprocess.Popen([
        QEMU, "-vga", "std", "-display", "none", "-m", "128",
        "-drive", "format=raw,file=" + IMG,
        "-drive", "format=raw,file=" + disk,
        "-qmp", "tcp:127.0.0.1:%d,server,nowait" % PORT,
        "-serial", "file:" + LOG,
    ], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    qmp = Qmp(PORT)
    if not wait_for(PROMPT, BOOT_TIMEOUT):
        p.kill()
        p.wait(timeout=15)
        raise RuntimeError("boot did not reach prompt")
    return p, qmp


def shutdown(p):
    p.kill()
    try:
        p.wait(timeout=15)
    except Exception:
        pass


def fresh_disk():
    shutil.copyfile(SRC, DISK)
    if os.path.getsize(DISK) < DISK_BYTES:
        with open(DISK, "r+b") as f:
            f.truncate(DISK_BYTES)


def build_gold(fsname, fmt_cmd, files):
    """格式化 + 放文件，存下黄金镜像。"""
    fresh_disk()
    p, qmp = launch(DISK)
    try:
        ok, out = run_cmd(qmp, fmt_cmd, timeout=120.0)
        rec("%s: %s" % (fsname, fmt_cmd), ok and "formatted" in out, out[:80])
        if not ok:
            return False
        for n, v in files:
            run_cmd(qmp, "write %s %s" % (n, v), timeout=30.0)
        time.sleep(0.5)
    finally:
        shutdown(p)
        shutil.copyfile(DISK, os.path.join(
            HERE, "fsrepair_gold_%s.img" % fsname).replace("\\", "/"))
    return True


# ---------------- 定向损伤（与 test_fsck.py 同源） ----------------

def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def vol_base(path):
    with open(path, "rb") as f:
        mbr = f.read(512)
    if len(mbr) < 512 or mbr[510:512] != b"\x55\xaa":
        return 0
    return _u32(mbr, 454) * 512


def make_fat32_orphan(dst):
    """空闲簇的 FAT 项改 EOC：分配表说占用、目录树没人指。"""
    base = vol_base(dst)
    with open(dst, "r+b") as f:
        f.seek(base)
        bpb = f.read(512)
        bps = struct.unpack_from("<H", bpb, 11)[0]
        reserved = struct.unpack_from("<H", bpb, 14)[0]
        if bps < 512 or (bps & (bps - 1)) or reserved == 0:
            return None
        fat0 = base + reserved * bps
        target = None
        for c in range(64, 4096):
            f.seek(fat0 + c * 4)
            b = f.read(4)
            if len(b) != 4:
                break
            if struct.unpack("<I", b)[0] == 0:
                target = c
                break
        if target is None:
            return None
        f.seek(fat0 + target * 4)
        f.write(struct.pack("<I", 0x0FFFFFF8))
    return target


def make_exfat_orphan(dst):
    """位图空闲簇的 bit 置 1（FAT 不动）。"""
    from ref_exfat import ExfatRW
    fs = ExfatRW(dst)
    target = None
    for c in range(2, 4096):
        if fs.fat(c) == 0 and not fs.bmp_get(c):
            target = c
            break
    if target is None:
        return None
    fs.bmp_set(target, 1)
    fs.flush()
    return target


def make_ext4_orphan_inode(dst):
    """把 HELLO.TXT 目录项的 inode 字段清零：目录树够不到，inode 还在。

    只动 4 字节 inode 字段，目录块其余内容（含 tail）不动——内核读目录
    不验目录块校验和，挂载行为不受影响。"""
    base = vol_base(dst)
    with open(dst, "r+b") as f:
        f.seek(base + 1024)
        sb = f.read(1024)
        if sb[0x38:0x3A] != b"\x53\xef":
            return None
        bs = 1024 << struct.unpack_from("<I", sb, 0x18)[0]
        ino_size = struct.unpack_from("<H", sb, 0x58)[0]
        # 1KB 块时主 GDT 在块 2
        f.seek(base + 2 * bs + 8)
        itab = struct.unpack("<I", f.read(4))[0]
        # inode 2 = 根目录；extent 首项在 inode+52：ee_block@0 ee_len@4
        # ee_start_hi@6 ee_start_lo@8 -> ee_start_lo 在 inode+60
        f.seek(base + itab * bs + 1 * ino_size + 52 + 8)
        root_blk = struct.unpack("<I", f.read(4))[0]
        f.seek(base + root_blk * bs)
        blk = f.read(bs)
        off = 0
        hit = None
        while off + 8 <= bs:
            rec_len = struct.unpack_from("<H", blk, off + 4)[0]
            if rec_len < 8 or (rec_len & 3) or off + rec_len > bs:
                return None
            name_len = blk[off + 6]
            name = blk[off + 8:off + 8 + name_len]
            if name.upper() == b"HELLO.TXT":
                hit = off
                break
            off += rec_len
        if hit is None:
            return None
        f.seek(base + root_blk * bs + hit)
        f.write(b"\x00\x00\x00\x00")
    return hit


# ---------------- 流程 ----------------

def check_fat_repair(fsname, fmt_cmd, files, corruptor):
    if not build_gold(fsname, fmt_cmd, files):
        return
    src = os.path.join(HERE, "fsrepair_gold_%s.img" % fsname).replace("\\", "/")
    shutil.copyfile(src, DISK)
    if corruptor(DISK) is None:
        rec("%s: corruptor found target" % fsname, False, "no target")
        return
    p, qmp = launch(DISK)
    try:
        ok, out = run_cmd(qmp, "fsck", timeout=CMD_TIMEOUT)
        rec("%s: fsck still reports orphan (read-only check intact)" % fsname,
            ok and "orphan=1" in out and "not consistent" in out, out[:160])
        ok, out = run_cmd(qmp, "fsck -a", timeout=CMD_TIMEOUT)
        rec("%s: fsck -a reclaims and rescans clean" % fsname,
            ok and "reclaimed 1 orphan cluster(s)" in out
            and "0 problem(s) -- consistent" in out, out[:200])
        ok, out = run_cmd(qmp, "fsck", timeout=CMD_TIMEOUT)
        rec("%s: fsck clean after repair" % fsname,
            ok and "0 problem(s) -- consistent" in out, out[:160])
    finally:
        shutdown(p)


def check_ext4_repair():
    if not build_gold("ext4", "format ext4", [("HELLO.TXT", "world")]):
        return
    src = os.path.join(HERE, "fsrepair_gold_ext4.img").replace("\\", "/")
    shutil.copyfile(src, DISK)
    if make_ext4_orphan_inode(DISK) is None:
        rec("ext4: corruptor found dirent", False, "no target")
        return
    p, qmp = launch(DISK)
    try:
        # 挂载即修复：boot 日志里必须有 repair note
        rec("ext4: mount-time repair note in boot log",
            wait_for(b"mount-time repair on ext4: reclaimed 1 orphan inode(s)", 30),
            "")
        ok, out = run_cmd(qmp, "ls", timeout=30.0)
        rec("ext4: orphaned file gone from ls",
            ok and "hello.txt" not in out and "error" not in out, out[:120])
        ok, out = run_cmd(qmp, "write NEW.TXT fresh", timeout=30.0)
        ok2, out2 = run_cmd(qmp, "cat NEW.TXT", timeout=30.0)
        rec("ext4: freed space usable for new file",
            ok and ok2 and "fresh" in out2, out2[:120])
        ok, out = run_cmd(qmp, "fsck -a", timeout=60.0)
        rec("ext4: fsck -a second pass reclaims nothing",
            ok and "reclaimed 0 orphan inode(s)" in out, out[:160])
    finally:
        shutdown(p)


def main():
    print("FSREPAIR: orphan reclaim must actually repair, not just detect")
    check_fat_repair("fat32", "format fat32",
                     [("A.TXT", "hello"), ("B.TXT", "world")], make_fat32_orphan)
    check_fat_repair("exfat", "format exfat",
                     [("A.TXT", "hello"), ("B.TXT", "world")], make_exfat_orphan)
    check_ext4_repair()

    bad = [n for (n, ok, _) in results if not ok]
    print("---- %d/%d passed ----" % (len(results) - len(bad), len(results)))
    if bad:
        for n in bad:
            print("  FAILED: %s" % n)
        print("FSREPAIR: FAIL")
        return 1
    print("FSREPAIR: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
