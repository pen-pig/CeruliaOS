# -*- coding: utf-8 -*-
"""probe_browser.py - 复现"浏览器点输入框就卡死"（2026-10-21 用户报障）

用户现象：浏览器里点地址栏（输入框）就冻结，"诸如此类的问题很多"。
VMware 串口日志显示最后一次启动干净（ACPI FADT OK / FS single-disk OK /
自检全过 / VBE thunk OK），但 GUI 交互冻结不写串口 —— 死循环/死锁，
无 panic。

本探针在 QEMU 复现并做冻结判定：
  1. boot -> desktop -> 开浏览器（'b'）
  2. 点地址栏 -> 打字 'x' -> 判定：
     a. 打的字是否出现在地址栏（画面变化）
     b. 移动鼠标后光标是否重绘（忙等 GUI 循环是否还活着）
        冻结 = 光标停止移动 / 打字不上屏
用法: python tests/probe_browser.py [bios|uefi]
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
LOG = os.path.join(ROOT, "temp", "browser_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()
BOOT_WAIT = 90.0


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
        for i, ch in enumerate(text):
            self.hmc("sendkey " + ("spc" if ch == " " else ch))
            time.sleep(0.03)
        self.hmc("sendkey ret")
        time.sleep(2.5)

    def key(self, k):
        self.hmc("sendkey " + k)
        time.sleep(0.15)

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
        time.sleep(0.12)
        self.hmc("mouse_button 0")
        time.sleep(0.25)

    def shot(self, path):
        rm(path)
        self.hmc("screendump " + path)
        time.sleep(0.35)
        return path


def ppm_load(fn):
    with open(fn, "rb") as f:
        data = f.read()
    parts = data.split(b"\n", 3)
    w, h = map(int, parts[1].split())
    return w, h, parts[3]


def win_bbox(buf, w, h):
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
    S = [os.path.join(ROOT, "temp", "browser_s%d.ppm" % i).replace("\\", "/")
         for i in range(10)]
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

        # open browser
        qmp.key("b")
        time.sleep(3.0)
        qmp.shot(S[1])
        _, _, d1 = ppm_load(S[1])
        bb = win_bbox(d1, w, h)
        results.append(("browser window visible", bb is not None))
        if bb is None:
            return 1
        x0, y0, x1, y1 = bb
        print("  browser bbox: %r (w=%d h=%d)" % (bb, x1 - x0, y1 - y0))

        # URL bar = toolbar top of content area, right of < > buttons.
        # win_bbox 的 y0 是白色页面区顶部；工具栏在页面区上方 BR_TOOLBAR_H=24px，
        # 地址栏中心约在 y0 - 12（gw_oy + BR_TOOLBAR_H/2）。
        # x 点地址栏左端（x0+31，内容区相对 x≈30，< > 按钮右侧）：
        # 此处内容区坐标与屏幕坐标必然分道（内容区 br_bar_x=27 vs 屏幕 86），
        # 才能区分坐标域实现的对错。
        bar_cx = x0 + 31
        bar_cy = y0 - 12
        print("  clicking URL bar at (%d,%d)" % (bar_cx, bar_cy))
        qmp.click(bar_cx, bar_cy)
        time.sleep(0.6)

        # type 'hello' into the focused input box
        for ch in "hello":
            qmp.key(ch)
            time.sleep(0.3)
        time.sleep(0.6)
        qmp.shot(S[2])

        # color/typing check: sample the FULL bar width for dark pixels (typed
        # chars = black on light focused bar). Count dark cells.
        # 文字基线在 gw_oy + (BR_TOOLBAR_H-8)/2 = y0 - 24 + 8 = y0 - 16
        # 空 URL 时键入的字符在栏左端（ax+3≈x0+29 起），必须扫整条栏宽
        _, _, d2c = ppm_load(S[2])
        light = dark = 0
        ybar = y0 - 16
        bx0, bx1 = x0 + 30, x1 - 50
        for x in range(bx0, bx1):
            o = (ybar * w + x) * 3
            lum = d2c[o] + d2c[o + 1] + d2c[o + 2]
            if lum > 350:   # #3C9BE8 lum=447, 旧阈值 500 误判为暗
                light += 1
            elif lum < 150:
                dark += 1
        # also scan the few rows around the text baseline for any dark cluster
        dark_rows = 0
        for yy in range(ybar - 4, ybar + 5):
            rowdark = 0
            for x in range(bx0, bx1):
                o = (yy * w + x) * 3
                if d2c[o] + d2c[o + 1] + d2c[o + 2] < 150:
                    rowdark += 1
            if rowdark > 3:
                dark_rows += 1
        bar_light = light > 100
        has_chars = dark_rows >= 2 and dark > 5
        results.append(("URL bar bg light (not black) on focus", bar_light,
                        "light=%d dark=%d darkrows=%d" % (light, dark, dark_rows)))
        results.append(("typed chars visible (dark clusters on bar)", has_chars,
                        "light=%d dark=%d darkrows=%d" % (light, dark, dark_rows)))

        # freeze detector 1: does typed char appear? (S1 before, S2 after type)
        # freeze detector 2: move mouse a lot, does cursor redraw?
        qmp.move_to(w - 200, h - 200)
        qmp.shot(S[3])
        qmp.move_to(w - 500, h - 400)
        qmp.shot(S[4])

        w3, h3, d3 = ppm_load(S[3])
        _, _, d4 = ppm_load(S[4])
        d_move, n_move = frame_diff(d3, d4, w3, h3)
        # 光标 ~16x16 移动 300px 只改 ~200px（<0.02%），不能按百分比判；
        # 只要两帧间有显著像素变化就是循环在重绘（活着）。
        mouse_alive = d_move > 30
        results.append(("GUI loop alive after input (mouse redraws)", mouse_alive,
                        "diff %d/%d" % (d_move, n_move)))
        if not mouse_alive:
            # 冻结：多次采样 EIP，区分死循环（卡同一地址）vs 忙等活着（游走）
            print("\n===== FROZEN EIP samples =====")
            addrs = []
            for _ in range(8):
                r = qmp.hmc("info registers")
                txt = r.get("return", "")
                for tok in txt.split():
                    if tok.startswith("EIP="):
                        addrs.append(tok.split("=")[1])
                        break
                time.sleep(0.2)
            print("EIP samples:", addrs)
            print("distinct:", len(set(addrs)),
                  "-> STUCK(infinite loop)" if len(set(addrs)) <= 2
                  else "-> SPINNING(busy-wait alive)")

        # typed char: compare bar area before/after type
        _, _, d1b = ppm_load(S[1])
        _, _, d2b = ppm_load(S[2])
        d_type, n_type = frame_diff(d1b, d2b, w, h)
        typed_visible = d_type * 200 > n_type  # >0.5% changed
        results.append(("typed char visible in input box", typed_visible,
                        "diff %d/%d = %.2f%%" % (d_type, n_type,
                                                 d_type * 100.0 / n_type)))

        # report
        print("\n===== probe_browser results =====")
        allok = True
        for r in results:
            name = r[0]
            ok = r[1]
            detail = r[2] if len(r) > 2 else ""
            print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                               (" | " + detail) if detail else ""))
            allok = allok and ok
        print("\nFROZEN" if (not mouse_alive or not typed_visible) else "\nALIVE")
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


if __name__ == "__main__":
    sys.exit(main())
