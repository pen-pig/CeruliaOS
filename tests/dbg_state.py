# -*- coding: utf-8 -*-
"""dbg_state.py - 逐操作打印「探针模型 vs 内核转储」的分歧点

用途：探针模型与内核一旦漂移，后面所有像素断言的前提都不可信。
本脚本把 C1（标题栏最小化按钮）整条链逐步 F12 转储，逐步打印
模型与内核的差异，用**机器判定**找到第一个分歧操作。

不依赖任何截图（0 次 screendump），所以不受沙箱文件操作阈值影响。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

from probe_combo import Qmp, ppm_load, px, TITLE_CY_OFF, START_BTN  # noqa: E402
from probe_fuzz import (FuzzModel, Fuzzer, GFX_W, GFX_H, DESK_BOTTOM,  # noqa: E402
                        APP_IDX, BOOT_WAIT, TB_Y)
from gui_probe import parse_gw_dump, park_mouse  # noqa: E402
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "dbgstate_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()

MIN_DX = -20
CLOSE_DX = -7


def rm(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
    except Exception:   # 沙箱 safe-delete 抛的非 OSError 也要吞
        pass


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
            print("BOOT FAILED")
            return 1
        qmp = Qmp(QMP_PORT)
        qmp.cx, qmp.cy = 640, 512
        qmp.line("desktop")
        time.sleep(35)

        m = FuzzModel()
        S = [os.path.join(ROOT, "temp", "dbgstate_s%d.ppm" % i).replace("\\", "/")
             for i in range(4)]
        shots = [0]
        f = Fuzzer(qmp, m, S, shots, log=LOG)
        f.ref = None

        def mine():
            out = []
            for s in m.used():
                w = m.slots[s]
                out.append("slot%d app=%d x=%d y=%d %dx%d hid=%d foc=%d max=%d"
                           % (s, APP_IDX[w["app"]], w["x"], w["y"], w["w"], w["h"],
                              1 if w["hidden"] else 0,
                              1 if s == m.focused else 0,
                              1 if w["max"] else 0))
            return out

        def kern():
            hdr, wins = f.gw_state()
            if hdr is None:
                return ["<no dump>"]
            out = ["n=%d focus=%d menu=%d mx=%d my=%d" %
                   (hdr["n"], hdr["focus"], hdr["menu"], hdr["mx"], hdr["my"])]
            for w in wins:
                out.append("slot%d app=%d x=%d y=%d %dx%d hid=%d foc=%d max=%d tbx=%d tw=%d"
                           % (w["slot"], w["app"], w["x"], w["y"], w["w"], w["h"],
                              w["hid"], w["foc"], w["max"], w["tbx"], w["tw"]))
            return out

        step = [0]

        def trace(tag):
            step[0] += 1
            print("\n=== #%d %s ===" % (step[0], tag), flush=True)
            print("  MODEL :", flush=True)
            for l in mine():
                print("    " + l, flush=True)
            print("  KERNEL:", flush=True)
            for l in kern():
                print("    " + l, flush=True)

        def park():
            """复刻 combo2 的 shot()：先停鼠标到停车位再截图。"""
            park_mouse(qmp)
            shots[0] += 1
            qmp.shot(S[0])
            return ppm_load(S[0])[2]

        def menu_open():
            qmp.click(*START_BTN)
            time.sleep(0.6)
            m.menu = True

        def launch(app):
            menu_open()
            qmp.click(248, 130 + APP_IDX[app] * 61 + 30)
            time.sleep(1.8)
            m.menu = False
            return m.launch(app)

        park()
        trace("boot: empty desktop")

        # --- 1. launch files ---
        launch("files")
        park()
        trace("after launch files")

        # --- 2. 点击 files 标题栏最小化钮 ---
        w = m.slots[0]
        qmp.click(w["x"] + w["w"] + MIN_DX, w["y"] + TITLE_CY_OFF)
        time.sleep(1.2)
        m.minimize(0)
        park()
        trace("after titlebar-min on files")

        # --- 3. 任务栏恢复 ---
        d = dict((s, (bx, tw)) for s, bx, tw in m.taskbar_buttons())
        bx, tw = d[0]
        qmp.click(bx + tw // 2, TB_Y)
        time.sleep(1.2)
        m.restore(0)
        park()
        trace("after taskbar restore files")

        # --- 4. launch notepad ---
        launch("notepad")
        park()
        trace("after launch notepad")

        # --- 5. 再点 files 标题栏最小化钮（非聚焦）---
        w = m.slots[0]
        qmp.click(w["x"] + w["w"] + MIN_DX, w["y"] + TITLE_CY_OFF)
        time.sleep(1.2)
        m.minimize(0)
        park()
        trace("after titlebar-min on NON-focused files")

        # --- 6. 任务栏恢复 hidden files ---
        d = dict((s, (bx, tw)) for s, bx, tw in m.taskbar_buttons())
        print("\n  model taskbar buttons: %r" % (m.taskbar_buttons(),))
        bx, tw = d[0]
        qmp.click(bx + tw // 2, TB_Y)
        time.sleep(1.2)
        m.restore(0)
        park()
        trace("after taskbar restore files (again)")

        # --- 7. 关 files（模型说它是 focused）---
        w = m.slots[0]
        print("\n  click close of files at (%d,%d)" %
              (w["x"] + w["w"] + CLOSE_DX, w["y"] + TITLE_CY_OFF))
        qmp.click(w["x"] + w["w"] + CLOSE_DX, w["y"] + TITLE_CY_OFF)
        time.sleep(1.2)
        m.close(0)
        park()
        trace("after close files")

        # --- 8. 关 notepad ---
        w = m.slots[1]
        print("\n  click close of notepad at (%d,%d)" %
              (w["x"] + w["w"] + CLOSE_DX, w["y"] + TITLE_CY_OFF))
        qmp.click(w["x"] + w["w"] + CLOSE_DX, w["y"] + TITLE_CY_OFF)
        time.sleep(1.2)
        m.close(1)
        park()
        trace("after close notepad")

        print("\nscreendumps used: %d" % shots[0])
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
