# -*- coding: utf-8 -*-
"""probe_vbe.py - 实模式回切 INT 10h 设 VBE 模式（bios_thunk）探针

2026-10-20 VMware 暴露：gfx_init 原来只认 Bochs VBE_DISPI 寄存器
（QEMU cirrus 专用），VMware SVGA / 真机 VESA BIOS 没有 DISPI，
desktop 直接落 VGA 0x13 回退。修复：boot.asm 探测时记下模式号，
gfx_init 在无 DISPI 时用 bios_thunk 实模式回切调 INT 10h 0x4F02，
读回 mode info 逐字段比对（fail closed）。

QEMU 的 -vga std 有 DISPI，平时走寄存器路径；本探针用
"vbetest force" 把 gfx_skip_dispi 置 1，强制 desktop 走 thunk
路径——VMware/真机路径在 QEMU 上的唯一可测入口。

断言：
  1. vbetest：BIOS 应答 0x004F + 读回几何 1280x1024x16 + 当前模式一致
  2. vbetest force + desktop：进 1280x1024 VBE 几何（不是 0x13 的
     320x200，也不是文本 640x480），且 Terminal 窗口可见
  3. 无输入两帧稳定（thunk 路径不闪）
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "vbe_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()
BOOT_WAIT = 90.0


def rec(results, name, ok, detail=""):
    results.append((name, bool(ok)))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       (" | " + detail) if detail else ""))


def rm(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


class Qmp(object):
    def __init__(self, port):
        import json
        import socket
        self.json = json
        for _ in range(80):
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), 2)
                break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("QMP connect failed")
        self.sock.settimeout(60)
        self.buf = b""
        self._readline()          # QMP greeting
        self.cmd("qmp_capabilities")

    def _readline(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("QMP EOF")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line

    def cmd(self, name, **args):
        self.sock.sendall((self.json.dumps({"execute": name,
                                            "arguments": args})
                           + "\n").encode())
        while True:
            line = self._readline()
            try:
                o = self.json.loads(line)
            except ValueError:
                continue
            if "return" in o or "error" in o:
                return o

    def hmc(self, c):
        return self.cmd("human-monitor-command", **{"command-line": c})

    def line(self, text):
        for i, ch in enumerate(text):
            self.hmc("sendkey " + ("spc" if ch == " " else ch))
            time.sleep(0.03)
            if i % 10 == 9:
                time.sleep(0.15)
        self.hmc("sendkey ret")
        time.sleep(3.0)

    def key(self, k):
        self.hmc("sendkey " + k)
        time.sleep(0.2)

    def shot(self, path):
        rm(path)
        self.hmc("screendump " + path)
        time.sleep(0.4)
        return path


def ppm_load(fn):
    with open(fn, "rb") as f:
        data = f.read()
    parts = data.split(b"\n", 3)
    w, h = map(int, parts[1].split())
    return w, h, parts[3]


def serial_text():
    try:
        return open(LOG, "rb").read().decode("latin-1", "replace")
    except OSError:
        return ""


def light_run_bbox(buf, w, h):
    """最长连续亮色 run -> (x0,y0,x1,y1)；终端窗口内容区是 #F3F3F3"""
    best = None
    for y in range(0, h - 30):
        base = y * w
        run = 0
        for x in range(w):
            o = (base + x) * 3
            if buf[o] > 225 and buf[o + 1] > 225 and buf[o + 2] > 225:
                run += 1
                if best is None or run > best[0]:
                    best = (run, x - run + 1, x, y)
            else:
                run = 0
    if best is None or best[0] < 200:
        return None
    _, x0, x1, yrow = best
    y0 = y1 = yrow
    while y0 - 1 >= 0:
        o = ((y0 - 1) * w + (x0 + x1) // 2) * 3
        if buf[o] > 225 and buf[o + 1] > 225 and buf[o + 2] > 225:
            y0 -= 1
        else:
            break
    while y1 + 1 < h:
        o = ((y1 + 1) * w + (x0 + x1) // 2) * 3
        if buf[o] > 225 and buf[o + 1] > 225 and buf[o + 2] > 225:
            y1 += 1
        else:
            break
    return (x0, y0, x1, y1)


def frame_diff(a, b, w, h, step=3):
    n = d = 0
    for y in range(0, h, step):
        base = y * w
        for x in range(0, w, step):
            o = (base + x) * 3
            n += 1
            if a[o:o + 3] != b[o:o + 3]:
                d += 1
    return d, n


def main():
    results = []
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    rm(LOG)
    proc = subprocess.Popen(
        [QEMU, "-machine", "pc", "-vga", "std", "-m", "128",
         "-drive", "format=raw,file=" + IMG,
         "-drive", "format=raw,file=" + DISK,
         "-display", "none", "-no-reboot",
         "-serial", "file:" + LOG,
         "-qmp", "tcp:127.0.0.1:%d,server,nowait" % QMP_PORT],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    S = [os.path.join(ROOT, "temp", "vbe_s%d.ppm" % i).replace("\\", "/")
         for i in range(4)]
    try:
        end = time.time() + BOOT_WAIT
        ok_shell = False
        while time.time() < end:
            if "[/] > " in serial_text():
                ok_shell = True
                break
            time.sleep(0.5)
        rec(results, "boot reaches shell", ok_shell)
        if not ok_shell:
            return 1
        qmp = Qmp(QMP_PORT)

        # ---- 1. vbetest：thunk 本体 ----
        qmp.line("vbetest")
        time.sleep(2.0)
        txt = serial_text()
        # vbetest 的输出行（print_hex32 无 0x 前缀，实测）：
        #   vbetest: set VBE mode 0000411A -> AX=0000004F OK, readback 1280x1024x16 LFB=FD000000 stride=2560 cur=0000411A VERIFIED
        has_ok = "-> AX=0000004F" in txt and "VERIFIED" in txt
        has_geo = "readback 1280x1024x16" in txt
        rec(results, "INT 10h thunk sets VBE mode + readback verified",
            has_ok and has_geo,
            "| AX ok=%s geometry ok=%s" % (has_ok, has_geo))
        if not (has_ok and has_geo):
            for ln in txt.splitlines():
                if "vbetest" in ln:
                    print("    serial: " + ln.strip())
            return 1

        # ---- 2. 强制 desktop 走 thunk 路径 ----
        qmp.line("vbetest force")
        time.sleep(2.0)
        txt = serial_text()
        rec(results, "force pins gfx_skip_dispi",
            "gfx_skip_dispi=1" in txt,
            "| flag line present=%s" % ("gfx_skip_dispi=1" in txt))
        if "gfx_skip_dispi=1" not in txt:
            return 1

        qmp.line("desktop")
        # thunk 路径（BIOS 设模式）在 TCG 下开机动画 + 首次全屏重绘可达
        # 分钟级（每个 LFB 写都触发 QEMU 全屏刷新），轮询等桌面就位，
        # 不要固定 sleep——DISPI 路径几秒、thunk 路径可能 2-3 分钟
        w = h = 0
        d0 = b""
        t_end = time.time() + 150
        while time.time() < t_end:
            qmp.shot(S[0])
            try:
                w, h, d0 = ppm_load(S[0])
            except (OSError, ValueError, IndexError):
                time.sleep(5)
                continue
            if (w, h) == (1280, 1024):
                break
            time.sleep(5)
        rec(results, "desktop on thunk path enters VBE geometry",
            (w, h) == (1280, 1024),
            "| %dx%d (expect 1280x1024; bug = 320x200 or 640x480)" % (w, h))
        if (w, h) != (1280, 1024):
            return 1

        # ---- 3. 开 Terminal 窗口 + 帧稳定 ----
        qmp.key("t")
        # 开窗后全屏重绘同样慢，轮询等近白内容区出现（Terminal #F3F3F3）
        bb = None
        w2 = h2 = 0
        d1 = b""
        t_end = time.time() + 90
        while time.time() < t_end:
            qmp.shot(S[1])
            try:
                w2, h2, d1 = ppm_load(S[1])
            except (OSError, ValueError, IndexError):
                time.sleep(5)
                continue
            bb = light_run_bbox(d1, w2, h2)
            if bb is not None:
                break
            time.sleep(5)
        rec(results, "terminal window visible on thunk path",
            bb is not None, "| bbox=%r" % (bb,))
        qmp.shot(S[2])
        _, _, d2 = ppm_load(S[2])
        d, n = frame_diff(d1, d2, w2, h2)
        rec(results, "no-input frames stable (thunk path)",
            d * 100 < n, "| diff %d/%d = %.2f%%" % (d, n, 100.0 * d / n))

        npass = sum(1 for _, ok in results if ok)
        print("%d/%d passed" % (npass, len(results)))
        return 0 if npass == len(results) else 1
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
