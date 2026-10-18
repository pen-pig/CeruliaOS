# -*- coding: utf-8 -*-
"""test_wm.py - 图形桌面窗口管理器回归（用户报的三症状：拖不动 / 闪 / 主题异常）

背景（2026-10-18）：gfx_init 的 VBE LFB 路径被一行 `0 &&`
（TEMP-FORCE-FALLBACK 调试残留）永久短路，desktop 命令落到 VGA 0x13
fallback；而 QEMU cirrus 在 DISPI 仍 enabled 时忽略标准 VGA 模式切换
（寄存器回读全对、屏幕不动），内核按 320x200 往 0xA0000 画 →
桌面全不可见、鼠标命中全错位、每帧重绘闪烁。用户侧现象就是
"窗口拖不动 / 屏幕一直闪 / 主题不正常"。

本测试把三个症状钉成断言：
  1. desktop 必须进 boot 探测的 VBE 几何（1280x1024）——退到 0x13
     回退（320x200）或文本模式（640x480）都直接红
  2. 无输入连拍两帧，帧间差异 < 1%——闪屏红
  3. 标题栏拖动 120px，窗口 bbox 必须真的移动——拖不动红
  4. 任务栏暗色 + 标题栏 accent 蓝——主题错红

用法：
    python tests/test_wm.py            # BIOS 路径
    python tests/test_wm.py uefi       # UEFI 路径
    python tests/test_wm.py bios:std   # 指定 -vga 型号
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
LOG = os.path.join(ROOT, "temp", "wm_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()
BOOT_WAIT = 90.0

# 断言锚点（2026-10-18 实测，改桌面配色/几何时要跟着重锚）：
#   任务栏带平均 (14,25,21)，标题栏 accent 蓝 (0,120,215) 9878/10092 像素，
#   标题栏右无字区 708/720 像素是 accent 蓝。
TB_MAX_CHANNEL = 60      # 任务栏任一通道均值上限（暗色）
TITLE_BLUE_MIN = 400     # 标题栏右无字区 accent 蓝像素下限（实测 708）


def rec(results, name, ok, detail=""):
    results.append((name, bool(ok)))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       (" | " + detail) if detail else ""))


def rm(path):
    """沙箱会拦 os.remove：一切删除带保护，否则断言全过后炸 finally
    （现象"最后一条 PASS 却退出码 1"）"""
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
        self.sock.settimeout(30)
        self.f = self.sock.makefile("rwb")
        self.f.readline()
        self.cmd("qmp_capabilities")
        self.cx, self.cy = 320, 240

    def cmd(self, name, **args):
        self.f.write((self.json.dumps({"execute": name, "arguments": args})
                      + "\n").encode())
        self.f.flush()
        while True:
            line = self.f.readline()
            if not line:
                return {}
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

    def move_to(self, x, y):
        while self.cx != x or self.cy != y:
            dx = max(-12, min(12, x - self.cx))
            dy = max(-12, min(12, y - self.cy))
            self.hmc("mouse_move %d %d" % (dx, dy))
            self.cx += dx
            self.cy += dy
            time.sleep(0.05)
        time.sleep(0.15)

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


def win_bbox(buf, w, h):
    """窗口内容底 #F3F3F3/白 的大块亮区 -> (x0,y0,x1,y1)；找不到返回 None。
    用"最长连续亮色 Run"定位，桌面图标（<=56px 宽）不会污染。"""
    best = None
    for y in range(0, h - 30):          # 排除底部任务栏
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
    # 上下扩展：同一 x 区间内仍是亮色的行
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


def zone_avg(buf, w, x0, y0, x1, y1, step=4):
    r = g = b = n = 0
    for y in range(y0, y1, step):
        for x in range(x0, x1, step):
            o = (y * w + x) * 3
            n += 1
            r += buf[o]; g += buf[o + 1]; b += buf[o + 2]
    return (r // n, g // n, b // n)


def accent_blue_count(buf, w, h, x0, y0, x1, y1):
    """accent 蓝 (0,120,215) 容差 +-60 的像素数"""
    n = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            o = (y * w + x) * 3
            if (abs(buf[o] - 0) <= 60 and abs(buf[o + 1] - 120) <= 60
                    and abs(buf[o + 2] - 215) <= 60):
                n += 1
    return n


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "bios"
    vga = "std"
    if path.startswith("bios:"):
        vga = path.split(":", 1)[1]
        path = "bios"
    results = []
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    rm(LOG)
    if path == "uefi":
        from ezos_env import qemu32_exe, ovmf_fd
        ESP = os.path.join(ROOT, "uefi/esp").replace("\\", "/")
        DISK_SRC = os.path.join(ROOT, "disk.vhd").replace("\\", "/")
        DISK2 = os.path.join(ROOT, "temp", "wm_disk.vhd").replace("\\", "/")
        import shutil
        shutil.copyfile(DISK_SRC, DISK2)
        proc = subprocess.Popen(
            [qemu32_exe(), "-machine", "pc", "-m", "256", "-vga", "std",
             "-icount", "shift=auto",
             "-drive", "if=pflash,format=raw,readonly=on,file=" + ovmf_fd(),
             "-drive", "if=none,format=raw,file=fat:rw:" + ESP + ",id=esp",
             "-usb", "-device", "usb-storage,drive=esp",
             "-drive", "format=raw,file=" + DISK2,
             "-serial", "file:" + LOG,
             "-display", "none",
             "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % QMP_PORT],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc = subprocess.Popen(
            [QEMU, "-machine", "pc", "-vga", vga, "-m", "128",
             "-drive", "format=raw,file=" + IMG,
             "-drive", "format=raw,file=" + DISK,
             "-display", "none", "-no-reboot",
             "-serial", "file:" + LOG,
             "-qmp", "tcp:127.0.0.1:%d,server,nowait" % QMP_PORT],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    qmp = None
    S = [os.path.join(ROOT, "temp", "wm_s%d.ppm" % i).replace("\\", "/")
         for i in range(6)]
    try:
        # ---- boot ----
        end = time.time() + BOOT_WAIT
        ok_shell = False
        while time.time() < end:
            try:
                if "[/] > " in open(LOG, "rb").read().decode("latin-1", "replace"):
                    ok_shell = True
                    break
            except OSError:
                pass
            time.sleep(0.5)
        rec(results, "boot reaches shell", ok_shell)
        if not ok_shell:
            return 1
        qmp = Qmp(QMP_PORT)
        if path == "uefi":
            # 吃 OVMF 键盘残留字节（同 test_uefi3）：否则首个命令被贴上杂字符
            qmp.line("ver")
            time.sleep(1.5)

        qmp.line("desktop")
        time.sleep(35)                    # 开机动画 + 首帧（UEFI TCG 更慢）
        qmp.shot(S[0])
        w, h, d0 = ppm_load(S[0])
        # 几何钉死：boot 探测的 VBE 模式。退 0x13（320x200）或文本（640x480）都红
        rec(results, "desktop entered at VBE geometry", (w, h) == (1280, 1024),
            "| %dx%d (expect 1280x1024)" % (w, h))
        if (w, h) != (1280, 1024):
            return 1
        # gw_start 的 mouse_warp 把光标放在屏幕正中心——QMP 坐标簿以此为准
        qmp.cx, qmp.cy = w // 2, h // 2

        # ---- open Terminal via keyboard shortcut ----
        qmp.key("t")
        time.sleep(2.5)
        qmp.shot(S[1])
        w, h, d1 = ppm_load(S[1])
        bb1 = win_bbox(d1, w, h)
        rec(results, "terminal window visible", bb1 is not None,
            "| bbox=%r" % (bb1,))
        if bb1 is None:
            return 1
        x0, y0, x1, y1 = bb1
        print("  window bbox: %r  (w=%d h=%d)" % (bb1, x1 - x0, y1 - y0))

        # ---- flicker: no input, two shots ----
        qmp.shot(S[2])
        time.sleep(1.2)
        qmp.shot(S[3])
        w2, h2, d2 = ppm_load(S[2])
        _, _, d3 = ppm_load(S[3])
        d, n = frame_diff(d2, d3, w, h)
        rec(results, "no-input frames stable (flicker)", d * 100 < n,
            "| diff %d/%d = %.2f%%" % (d, n, d * 100.0 / n))

        # ---- drag: title bar, +120px right ----
        # bbox 是内容区；窗框外顶 = y0-13，标题栏中心 = y0-7（GW_TITLE_H=12）
        tx, ty = (x0 + x1) // 2, y0 - 7
        qmp.move_to(tx, ty)
        qmp.hmc("mouse_button 1")
        time.sleep(0.3)
        for i in range(10):
            qmp.hmc("mouse_move 12 0")
            time.sleep(0.06)
        time.sleep(0.5)
        qmp.hmc("screendump " + S[4])
        time.sleep(0.4)
        wM, hM, dM = ppm_load(S[4])
        bb2 = win_bbox(dM, wM, hM)
        rec(results, "window still visible mid-drag", bb2 is not None,
            "| bbox=%r" % (bb2,))
        if bb2 is None:
            return 1
        qmp.hmc("mouse_button 0")
        time.sleep(1.0)
        qmp.shot(S[5])
        w5, h5, d5 = ppm_load(S[5])
        bb3 = win_bbox(d5, w5, h5)
        rec(results, "window still visible after release", bb3 is not None,
            "| bbox=%r" % (bb3,))
        if bb3 is None:
            return 1
        dx = bb3[0] - bb1[0]
        rec(results, "window moved right ~120px", 80 <= dx <= 160,
            "| dx=%d (expect ~120)" % dx)

        # ---- theme: taskbar dark + titlebar accent blue ----
        tb = zone_avg(d1, w, 0, h - 26, w, h - 2)
        dark_ok = max(tb) < TB_MAX_CHANNEL
        rec(results, "taskbar dark", dark_ok,
            "| avg=%r (max channel < %d)" % (tb, TB_MAX_CHANNEL))
        # 标题栏右端无字区（避开标题文字）：y0-13..y0-1, x1-80..x1-20
        blue = accent_blue_count(d1, w, h, x1 - 80, y0 - 13, x1 - 20, y0 - 1)
        rec(results, "titlebar accent blue", blue > TITLE_BLUE_MIN,
            "| %d px (expect > %d)" % (blue, TITLE_BLUE_MIN))

        failed = [n for n, ok in results if not ok]
        print("\n%d/%d passed" % (len(results) - len(failed), len(results)))
        for n in failed:
            print("  FAILED: " + n)
        return 1 if failed else 0
    finally:
        if qmp is not None:
            try:
                qmp.cmd("quit")
            except Exception:
                pass
        time.sleep(1)
        try:
            proc.kill()
        except OSError:
            pass
        for p in S + [LOG, os.path.join(ROOT, "temp", "wm_disk.vhd").replace("\\", "/")]:
            if not os.environ.get("WM_KEEP"):
                rm(p)


if __name__ == "__main__":
    sys.exit(main())
