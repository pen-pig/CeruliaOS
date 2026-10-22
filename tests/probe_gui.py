# -*- coding: utf-8 -*-
"""probe_gui.py - 图形桌面综合交互回归（浏览器输入框修复后的"诸此类"扫描）

覆盖 test_wm.py（拖动/主题/任务栏释按）与 probe_browser.py（地址栏）之外
的 GUI 交互路径。策略：**单窗口串行**——每步只开一个窗口、用已知 spawn
位置（cascade 0 = (60,51)，内容区 (61,64)）算坐标，不用 win_bbox 猜
（多窗口重叠时最长亮 run 会退化，2026-10-22 实测 bbox 退成 1px 高）。

  1. 桌面图标双击启动 File Manager
  2. 标题栏关闭按钮关窗
  3. 开始菜单启动 Notepad + 键盘打字上屏
  4. 标题栏最小化 + 任务栏按钮恢复
  5. 任务栏点聚焦窗口 = 最小化（再点恢复）
  6. 开始菜单启动 Settings + 主题行点击切换（Win10<->GNOME 布局变）

几何锚点（Win10 主题 1280x1024，gw_scale_x(v)=GFX_W*v/640 即 x2）：
  任务栏 y=992..1024(h=32)  开始按钮 x<48 → 点 (24,1008)
  图标网格 x0=16 y0=21 box=106 cols=4 → 图标0中心(69,74) 图标2(记事本)(313,74)
  开始菜单 (12,14)-(612,990)，列表 list_y=129，row_h=72，列表 x=12..484
  窗口 cascade 0：spawn(60,51) 840x640，内容区(61,64)-(899,690)
  标题栏 y=51..63；关闭钮 x=888..899，最小化钮 x=875..886
  任务栏窗口按钮起于 x=48，宽=8+len(title)*8+8
用法: python tests/probe_gui.py
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
LOG = os.path.join(ROOT, "temp", "gui_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()
BOOT_WAIT = 90.0

# 各 app 窗口尺寸（gw_spawn_app 的 switch，gw_scale_x(v)=GFX_W*v/640=2v，
# gw_scale_y(v)=GFX_H*v/480≈2.133v；cascade 0 spawn 于 (60,51)）
APP_WW = {"files": 880, "notepad": 760, "settings": 680, "browser": 1000}
WIN_X, WIN_Y = 60, 51
TITLE_CY = WIN_Y + 6                      # 标题栏中心 57


def btn_close_x(app):
    """关闭钮中心 x = wx + ww - 12 + 5（绘制 bx=x+ww-13 宽11，命中 bx=x+ww-12）"""
    return WIN_X + APP_WW[app] - 7


def btn_min_x(app):
    """最小化钮中心 x = wx + ww - 12 - 7"""
    return WIN_X + APP_WW[app] - 19


def content_rect(app):
    """内容区 (x0,y0,x1,y1)：x+1, y+13, x+ww-1, y+wh-1"""
    ww = APP_WW[app]
    return (WIN_X + 1, WIN_Y + 13, WIN_X + ww - 1, WIN_Y + 640 - 1)


START_BTN = (24, 1008)                    # 开始按钮（任务栏 x<48）
ICON0 = (69, 74)                          # 桌面图标0 = File Manager
MENU_LIST_X = 248                         # 开始菜单列表区中心（x=12..484）
# 菜单行高会被 gw_menu_row_h 压缩：默认 gw_scale_y(34)=72，但 12 行放不下
# （115+12*72=979 > mh-gw_scale_y(60)=847）时逐行递减，停在 61
# （115+12*61=847 不 > 847）。list_y = my0(15) + gw_scale_y(54)(115) = 130
MENU_LIST_Y0 = 130
MENU_ROW_H = 61


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
        for ch in text:
            self.hmc("sendkey " + ("spc" if ch == " " else ch))
            time.sleep(0.03)
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
            time.sleep(0.04)
        time.sleep(0.12)

    def click(self, x, y):
        self.move_to(x, y)
        self.hmc("mouse_button 1")
        time.sleep(0.1)
        self.hmc("mouse_button 0")
        time.sleep(0.25)

    def dblclick(self, x, y):
        """两次按下间隔 <400ms（内核双击判据 g_pit_ticks 差 <400）"""
        self.move_to(x, y)
        self.hmc("mouse_button 1")
        time.sleep(0.06)
        self.hmc("mouse_button 0")
        time.sleep(0.06)
        self.hmc("mouse_button 1")
        time.sleep(0.06)
        self.hmc("mouse_button 0")
        time.sleep(0.4)

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
    """单窗口场景：窗口内容底的大块亮区 -> (x0,y0,x1,y1)。"""
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


def frame_diff(a, b, w, h, step=1):
    n = d = 0
    for y in range(0, h, step):
        base = y * w
        for x in range(0, w, step):
            o = (base + x) * 3
            n += 1
            if a[o:o + 3] != b[o:o + 3]:
                d += 1
    return d, n


def zone_dark(buf, w, x0, y0, x1, y1):
    """区域内暗像素（文字）数"""
    n = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            o = (y * w + x) * 3
            if buf[o] + buf[o + 1] + buf[o + 2] < 200:
                n += 1
    return n


def taskbar_avg(buf, w, h):
    r = g = b = n = 0
    for y in range(h - 30, h - 2, 2):
        for x in range(0, w, 8):
            o = (y * w + x) * 3
            r += buf[o]; g += buf[o + 1]; b += buf[o + 2]; n += 1
    return (r // n, g // n, b // n)


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
    qmp = None
    S = [os.path.join(ROOT, "temp", "gui_s%d.ppm" % i).replace("\\", "/")
         for i in range(16)]
    try:
        end = time.time() + BOOT_WAIT
        ok = False
        while time.time() < end:
            try:
                if "[/] > " in open(LOG, "rb").read().decode("latin-1", "replace"):
                    ok = True
                    break
            except OSError:
                pass
            time.sleep(0.5)
        results.append(("boot reaches shell", ok))
        if not ok:
            return 1
        qmp = Qmp(QMP_PORT)
        qmp.cx, qmp.cy = 640, 512

        qmp.line("desktop")
        time.sleep(35)
        qmp.shot(S[0])
        w, h, d0 = ppm_load(S[0])
        results.append(("desktop entered", (w, h) == (1280, 1024)))
        if (w, h) != (1280, 1024):
            return 1
        qmp.cx, qmp.cy = w // 2, h // 2
        tb0 = taskbar_avg(d0, w, h)
        results.append(("taskbar dark (Win10 theme)", max(tb0) < 60,
                        "| avg=%r" % (tb0,)))

        # ---- 1. desktop icon double-click launches File Manager ----
        qmp.dblclick(*ICON0)
        time.sleep(2.5)
        qmp.shot(S[1])
        _, _, d1 = ppm_load(S[1])
        bb = win_bbox(d1, w, h)
        results.append(("icon dblclick launches File Manager", bb is not None,
                        "| bbox=%r (expect ~(58,80,933,690))" % (bb,)))
        if bb is None:
            return 1

        # ---- 2. close button closes window ----
        qmp.click(btn_close_x("files"), TITLE_CY)
        time.sleep(1.2)
        qmp.shot(S[2])
        _, _, d2 = ppm_load(S[2])
        bb2 = win_bbox(d2, w, h)
        results.append(("close button closes window", bb2 is None,
                        "| bbox after close=%r (expect None)" % (bb2,)))

        # ---- 3. start menu launches Notepad + typing ----
        qmp.click(*START_BTN)
        time.sleep(0.6)
        qmp.click(MENU_LIST_X, MENU_LIST_Y0 + 1 * MENU_ROW_H + MENU_ROW_H // 2)
        time.sleep(2.5)
        qmp.shot(S[3])
        _, _, d3 = ppm_load(S[3])
        bb3 = win_bbox(d3, w, h)
        results.append(("start menu launches Notepad", bb3 is not None,
                        "| bbox=%r" % (bb3,)))
        if bb3 is None:
            return 1
        # 点内容区聚焦（文本区内），再打字
        npx0, npy0, npx1, npy1 = content_rect("notepad")
        qmp.click(npx0 + 300, npy0 + 100)
        time.sleep(0.3)
        qmp.shot(S[4])
        _, _, d4 = ppm_load(S[4])
        dark_before = zone_dark(d4, w, npx0 + 2, npy0 + 15, npx0 + 300, npy0 + 60)
        for ch in "Ab1":
            qmp.key(ch)
            time.sleep(0.3)
        time.sleep(0.5)
        qmp.shot(S[5])
        _, _, d5 = ppm_load(S[5])
        dark_after = zone_dark(d5, w, npx0 + 2, npy0 + 15, npx0 + 300, npy0 + 60)
        results.append(("notepad typing shows text",
                        dark_after > dark_before + 8,
                        "| dark %d -> %d" % (dark_before, dark_after)))

        # ---- 4. minimize + taskbar restore ----
        qmp.click(btn_min_x("notepad"), TITLE_CY)
        time.sleep(1.2)
        qmp.shot(S[6])
        _, _, d6 = ppm_load(S[6])
        bb6 = win_bbox(d6, w, h)
        results.append(("minimize hides window", bb6 is None,
                        "| bbox after min=%r (expect None)" % (bb6,)))
        qmp.click(84, 1008)
        time.sleep(1.5)
        qmp.shot(S[7])
        _, _, d7 = ppm_load(S[7])
        bb7 = win_bbox(d7, w, h)
        results.append(("taskbar button restores window", bb7 is not None,
                        "| bbox=%r" % (bb7,)))

        # ---- 5. taskbar click on focused window = minimize ----
        qmp.click(84, 1008)
        time.sleep(1.2)
        qmp.shot(S[8])
        _, _, d8 = ppm_load(S[8])
        bb8 = win_bbox(d8, w, h)
        results.append(("taskbar click minimizes focused window", bb8 is None,
                        "| bbox=%r (expect None)" % (bb8,)))
        qmp.click(84, 1008)
        time.sleep(1.5)
        qmp.shot(S[9])
        _, _, d9 = ppm_load(S[9])
        bb9 = win_bbox(d9, w, h)
        results.append(("taskbar click again restores", bb9 is not None,
                        "| bbox=%r" % (bb9,)))

        # ---- 6. Settings theme switch ----
        # 先关 Notepad
        qmp.click(btn_close_x("notepad"), TITLE_CY)
        time.sleep(1.2)
        qmp.shot(S[10])
        _, _, d10 = ppm_load(S[10])
        results.append(("close Notepad clean", win_bbox(d10, w, h) is None,
                        "| bbox=%r" % (win_bbox(d10, w, h),)))
        # 开始菜单 → Settings（app4）
        qmp.click(*START_BTN)
        time.sleep(0.6)
        qmp.click(MENU_LIST_X, MENU_LIST_Y0 + 4 * MENU_ROW_H + MENU_ROW_H // 2)
        time.sleep(2.5)
        qmp.shot(S[11])
        _, _, d11 = ppm_load(S[11])
        bb_st = win_bbox(d11, w, h)
        results.append(("start menu launches Settings", bb_st is not None,
                        "| bbox=%r" % (bb_st,)))
        if bb_st is not None:
            # 主题行：内容区相对 y = 30 + row*18 + 9
            stx0, sty0, _, _ = content_rect("settings")
            qmp.click(stx0 + 200, sty0 + 30 + 1 * 18 + 9)
            time.sleep(2.0)
            qmp.shot(S[12])
            _, _, d12 = ppm_load(S[12])
            d_th, n_th = frame_diff(d11, d12, w, h, step=4)
            results.append(("theme click switches layout (Win10->GNOME)",
                            d_th * 10 > n_th,
                            "| diff %d/%d" % (d_th, n_th)))
            # 切回 Win10
            qmp.click(stx0 + 200, sty0 + 30 + 0 * 18 + 9)
            time.sleep(2.0)
            qmp.shot(S[13])
            _, _, d13 = ppm_load(S[13])
            tb2 = taskbar_avg(d13, w, h)
            d_back, n_back = frame_diff(d12, d13, w, h, step=4)
            results.append(("theme click switches back (GNOME->Win10)",
                            max(tb2) < 60 and d_back * 10 > n_back,
                            "| taskbar=%r diff %d/%d" % (tb2, d_back, n_back)))
            # 关 Settings
            qmp.click(btn_close_x("settings"), TITLE_CY)
            time.sleep(1.0)
            qmp.shot(S[14])
            _, _, d14 = ppm_load(S[14])
            results.append(("close Settings clean", win_bbox(d14, w, h) is None,
                            "| bbox=%r" % (win_bbox(d14, w, h),)))

        # report
        print("\n===== probe_gui results =====")
        allok = True
        for r in results:
            name = r[0]
            okv = r[1]
            detail = r[2] if len(r) > 2 else ""
            print("%s %s%s" % ("PASS" if okv else "FAIL", name,
                               (" | " + detail) if detail else ""))
            allok = allok and okv
        print("\nALL PASS" if allok else "\nHAS FAILURES")
        return 0 if allok else 1
    finally:
        try:
            if qmp:
                qmp.cmd("quit")
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            pass
        for p in S:
            rm(p)


if __name__ == "__main__":
    sys.exit(main())
