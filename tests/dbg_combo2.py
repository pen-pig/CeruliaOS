# -*- coding: utf-8 -*-
"""dbg_combo2.py - 复现 probe_combo2 C2 场景，逐步 dump 窗口实际位置

用法：python tests/dbg_combo2.py
每步打印：模型预期的标题栏中心坐标 + 屏幕上实际找到的蓝/灰标题栏包围盒。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

from probe_combo import Qmp, ppm_load, px, rm  # noqa: E402
from probe_chain import title_blue_exact, is_title_gray  # noqa: E402
from probe_fuzz import (FuzzModel, Fuzzer, APP_IDX, MENU_LIST_Y, MENU_ROW_H,  # noqa: E402
                        MENU_ROW_X, BOOT_WAIT)
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "dbg_combo2.log").replace("\\", "/")
QMP_PORT = alloc_port()


def bands(buf, w, h):
    """找出所有标题栏带：按行扫描蓝/灰像素，返回 [(y0,y1,x0,x1,kind)]。"""
    out = []
    cur = None
    for y in range(0, min(h, 992)):
        xs = []
        for x in range(0, w, 2):
            c = px(buf, w, x, y)
            if title_blue_exact(c) or is_title_gray(c):
                xs.append(x)
        if len(xs) > 40:
            if cur and cur[1] == y - 1:
                cur[1] = y
                cur[2] = min(cur[2], xs[0])
                cur[3] = max(cur[3], xs[-1])
            else:
                if cur:
                    out.append(cur)
                cur = [y, y, xs[0], xs[-1]]
        else:
            if cur:
                out.append(cur)
                cur = None
    if cur:
        out.append(cur)
    return out


def main():
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
    S = os.path.join(ROOT, "temp", "dbgc2.ppm").replace("\\", "/")
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
        if not ok:
            print("boot failed")
            return 1
        qmp = Qmp(QMP_PORT)
        qmp.cx, qmp.cy = 640, 512
        qmp.line("desktop")
        time.sleep(35)
        w, h, buf = ppm_load(qmp.shot(S))
        qmp.cx, qmp.cy = w // 2, h // 2
        m = FuzzModel()

        def step(tag):
            _, _, buf = ppm_load(qmp.shot(S))
            print("\n--- %s ---" % tag)
            print("  model: %r" % ([(s, m.slots[s]["app"], m.slots[s]["x"],
                                     m.slots[s]["y"], m.slots[s]["w"],
                                     m.slots[s]["h"], m.slots[s]["hidden"])
                                    for s in m.used()],))
            print("  focus=%r menu=%r" % (m.focused, m.menu))
            for (y0, y1, x0, x1) in bands(buf, w, h):
                print("  band y=%d..%d x=%d..%d  px(%d,%d)=%r" %
                      (y0, y1, x0, x1, (x0 + x1) // 2, (y0 + y1) // 2,
                       px(buf, w, (x0 + x1) // 2, (y0 + y1) // 2)))

        def menu_open():
            qmp.click(24, 1008)
            time.sleep(0.6)
            m.menu = True

        def menu_row(i):
            qmp.click(MENU_ROW_X, MENU_LIST_Y + i * MENU_ROW_H + MENU_ROW_H // 2)
            time.sleep(1.8)
            m.menu = False

        def launch(app):
            menu_open()
            menu_row(APP_IDX[app])
            return m.launch(app)

        # ---- 先复刻 probe_combo2 的 C1（找出状态污染源）----
        launch("files")            # slot0 (60,51,880x640)
        qmp.click(60 + 880 - 20, 51 + 6)     # 标题栏最小化钮
        time.sleep(1.0)
        m.minimize(0)
        step("C1a after titlebar-min files")
        bx, tw = dict((s, (b[1], b[2])) for s, b in
                      enumerate(m.taskbar_buttons()))[0]
        qmp.click(bx + tw // 2, 1008)        # 任务栏恢复
        time.sleep(1.2)
        m.restore(0)
        step("C1a after taskbar restore files")
        launch("notepad")          # slot1
        qmp.click(60 + 880 - 20, 51 + 6)     # files 的最小化钮（非聚焦）
        time.sleep(1.0)
        m.minimize(0)
        step("C1b after titlebar-min files (non-focused)")
        # round_end 等价清理
        f = Fuzzer(qmp, m, [S] * 33, [0])
        f.round_end("C1 round")
        step("C1 after round_end")

        launch("sysinfo")
        step("after launch sysinfo")
        launch("notepad")
        step("after launch notepad")
        qmp.drag(90, 57, 110, 94)
        time.sleep(0.8)
        m.bring_front(0)
        m.slots[0]["x"] = 60 + (110 - 90)
        m.slots[0]["y"] = 51 + (94 - 57)
        step("after drag sysinfo -> (80,88)")
        qmp.click(496, 101)
        time.sleep(0.8)
        step("after click overlap (496,101)")
        qmp.hmc("sendkey alt-tab")
        time.sleep(0.6)
        m.alt_tab()
        step("after alt-tab")
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
