# -*- coding: utf-8 -*-
"""probe_repro.py - 复现 probe_fuzz 三个未归因 FAIL 的现场（一次性诊断）

A2（8 窗 slot 打满后 focused 标题栏不是蓝）、A3a（任务栏/桌面点击序列后
焦点与模型不符）、fuzz#80（随机流中 focused 标题栏不是蓝）。

诊断方法：每步操作后截图 + 转储"聚焦标题栏蓝像素包围盒"（r<5 且 g/b 在
标题栏区间、排除图标矩形与任务栏的像素的 min/max x/y）——包围盒直接给出
内核实际绘制的聚焦标题栏位置，与模型预期位置一比，失步/漂移/漏绘当场
现形。不删截图（沙箱文件操作阈值）。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

from probe_combo import Qmp, ppm_load, px, rm, APP_WH, TITLE_CY_OFF  # noqa: E402
from probe_fuzz import (FuzzModel, APP_IDX, GFX_W, GFX_H,  # noqa: E402
                        DESK_BOTTOM, MENU_LIST_Y, MENU_ROW_H,
                        MENU_ROW_X, TB_Y, icon_rects)
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "repro_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()
OUT = os.path.join(ROOT, "temp", "repro")


def blue_bbox(buf, w, h):
    """聚焦标题栏蓝像素包围盒（排除图标/任务栏）。返回 (n, x0, y0, x1, y1)。"""
    icons = icon_rects()
    n = 0
    x0 = y0 = 10 ** 9
    x1 = y1 = -1
    for y in range(0, DESK_BOTTOM, 2):
        for x in range(0, GFX_W, 2):
            if any(ix <= x < ix + iw and iy <= y < iy + ih
                   for ix, iy, iw, ih in icons):
                continue
            o = (y * w + x) * 3
            c = (buf[o], buf[o + 1], buf[o + 2])
            if c[0] < 5 and 95 <= c[1] <= 140 and 185 <= c[2] <= 235:
                n += 1
                if x < x0:
                    x0 = x
                if x > x1:
                    x1 = x
                if y < y0:
                    y0 = y
                if y > y1:
                    y1 = y
    if n == 0:
        return 0, None, None, None, None
    return n, x0, y0, x1, y1


def main():
    os.makedirs(OUT, exist_ok=True)
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
    shots = [0]
    try:
        end = time.time() + 90.0
        ok = False
        while time.time() < end:
            try:
                if "[/] > " in open(LOG, "rb").read().decode("latin-1", "replace"):
                    ok = True
                    break
            except OSError:
                pass
            time.sleep(0.5)
        print("boot:", "ok" if ok else "FAIL", flush=True)
        if not ok:
            return 1
        qmp = Qmp(QMP_PORT)
        qmp.cx, qmp.cy = 640, 512
        qmp.line("desktop")
        time.sleep(35)
        w, h, _ = ppm_load(qmp.shot(os.path.join(OUT, "s00.ppm")))
        print("desktop:", (w, h), flush=True)

        def shot(name):
            p = os.path.join(OUT, name)
            qmp.shot(p)
            shots[0] += 1
            _, _, buf = ppm_load(p)
            return buf

        def report(tag, buf, extra=""):
            n, x0, y0, x1, y1 = blue_bbox(buf, w, h)
            print("%-28s blue: n=%-5d bbox=%r %s" %
                  (tag, n, (x0, y0, x1, y1), extra), flush=True)

        def menu_row(i):
            qmp.click(MENU_ROW_X, MENU_LIST_Y + i * MENU_ROW_H + MENU_ROW_H // 2)
            time.sleep(1.8)

        def launch(app):
            qmp.click(24, 1008)
            time.sleep(0.6)
            menu_row(APP_IDX[app])

        m = FuzzModel()

        # ============ S1: A2 序列——8 次菜单 launch，每步转储 ============
        print("\n--- S1: A2 sequence (8 menu launches) ---", flush=True)
        for i, app in enumerate(["files", "notepad", "sysinfo", "clock",
                                 "about", "terminal", "paint", "settings"]):
            launch(app)
            r = m.launch(app)
            buf = shot("s1_%d.ppm" % i)
            s = m.find_app(APP_IDX[app])
            tc = m.title_c(s) if s is not None else None
            px_c = px(buf, w, tc[0], tc[1]) if tc else None
            report("after launch#%d %s(%s)" % (i, app, r), buf,
                   "model slot=%s title_c=%r px=%r" % (s, tc, px_c))

        # 全关（taskbar 恢复 + 关窗），验桌面
        print("\n--- S1 close-all ---", flush=True)
        qmp.click(24, 1008)
        time.sleep(0.6)
        for s in list(m.used()):
            if m.slots[s]["hidden"]:
                pass
        # 简化：直接点每个可见窗的关闭钮（自顶而下）
        for _ in range(10):
            vis = [s for s in m.visible()]
            if not vis:
                break
            s = max(vis)
            cx, cy = m.close_pt(s)
            qmp.click(cx, cy)
            time.sleep(1.0)
            m.close(s)
        for s in list(m.used()):
            if m.slots[s] and m.slots[s]["hidden"]:
                m.close(s)
        buf = shot("s1_clean.ppm")
        report("S1 after close-all", buf, "model used=%r" % (m.used(),))

        # ============ S2: A3 序列——任务栏/桌面点击逐步 ============
        print("\n--- S2: A3 sequence (taskbar/desktop clicks) ---", flush=True)
        m = FuzzModel()
        launch("files")
        m.launch("files")
        launch("notepad")
        m.launch("notepad")
        buf = shot("s2_launched.ppm")
        report("S2 launched files+notepad", buf,
               "focus=%r files_title=%r" % (m.focused, m.title_c(0)))

        # 内核语义验证：notepad 是 focused，点 files 钮 = 恢复置顶（不是最小化）
        qmp.click(104, TB_Y)
        time.sleep(1.2)
        buf = shot("s2_click_files_btn.ppm")
        report("S2 click files btn (notepad focused)", buf,
               "kernel expect: files restored+focused (slot0)")
        qmp.click(104, TB_Y)
        time.sleep(1.2)
        buf = shot("s2_click_files_btn2.ppm")
        report("S2 click files btn again", buf,
               "kernel expect: files minimized (focused+visible)")
        qmp.click(200, TB_Y)
        time.sleep(1.2)
        buf = shot("s2_click_np_btn.ppm")
        report("S2 click notepad btn", buf,
               "kernel expect: notepad restored+focused")
        qmp.click(200, TB_Y)
        time.sleep(1.2)
        buf = shot("s2_click_np_btn2.ppm")
        report("S2 click notepad btn again", buf,
               "kernel expect: notepad minimized (focused+visible)")
        qmp.click(1000, 300)
        time.sleep(1.2)
        buf = shot("s2_desktop1.ppm")
        report("S2 desktop click #1", buf,
               "kernel expect: restore lowest hidden = files(slot0)")
        buf = shot("s2_desktop1b.ppm")   # 同一状态连拍两张（查时序谜）
        report("S2 desktop click #1 (re-shot)", buf, "should equal previous")
        qmp.click(1000, 300)
        time.sleep(1.2)
        buf = shot("s2_desktop2.ppm")
        report("S2 desktop click #2", buf,
               "kernel expect: restore notepad(slot1)")

        print("\nshots used: %d" % shots[0])
        return 0
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
