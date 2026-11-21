# -*- coding: utf-8 -*-
"""dbg_c2.py - 复现 combo2 C1→C2 的内核状态漂移（逐步 F12 转储）

combo2_final 的 C2 现象：两次菜单启动都没开出窗口（像素上只有壁纸），
但探针模型以为开出了 sysinfo+notepad。C3/C4 的启动也失败，C5/C6/C7
又时好时坏 —— 说明是**状态相关**的，不是菜单几何本身的问题
（dbg_menu.py 已证明：干净桌面上 menu→row 一次成功）。

这里照抄 combo2 的 C1 步骤序列，每步之后 F12 转储，定位"从哪一步开始
内核和探针模型分家"。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

from probe_combo import Qmp, TITLE_CY_OFF, START_BTN  # noqa: E402
from probe_fuzz import (Fuzzer, FuzzModel, APP_IDX, BOOT_WAIT, TB_Y,  # noqa: E402
                        MENU_ROW_X, MENU_LIST_Y, MENU_ROW_H)
from gui_probe import park_mouse  # noqa: E402
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "dbgc2_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()


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
        S = [os.path.join(ROOT, "temp", "dbgc2_s%d.ppm" % i).replace("\\", "/")
             for i in range(4)]
        shots = [0]
        f = Fuzzer(qmp, m, S, shots, log=LOG)

        def dump(tag):
            hdr, wins = f.gw_state()
            if hdr is None:
                print("%-34s NO DUMP" % tag, flush=True)
                return None, []
            print("%-34s menu=%d n=%d focus=%s wins=%r" %
                  (tag, hdr["menu"], hdr["n"], hdr["focus"],
                   [(w["slot"], w["app"], w["x"], w["y"], w["hid"], w["foc"],
                     w["max"]) for w in wins]), flush=True)
            return hdr, wins

        def menu_open():
            qmp.click(*START_BTN)
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

        park_mouse(qmp)
        dump("S0 park")

        # ---- C1a：launch files，点标题栏最小化钮 ----
        launch("files")
        dump("S1 after launch files")
        w0 = m.slots[0]
        qmp.click(w0["x"] + w0["w"] - 20, w0["y"] + TITLE_CY_OFF)
        time.sleep(1.0)
        m.minimize(0)
        dump("S2 after titlebar-min")

        # 任务栏恢复
        bx, tw = [(s, b, t) for s, b, t in m.taskbar_buttons()][0][1:]
        qmp.click(bx + tw // 2, TB_Y)
        time.sleep(1.2)
        m.restore(0)
        dump("S3 after taskbar restore")

        # ---- C1b：launch notepad，点 files 的最小化钮（非聚焦）----
        launch("notepad")
        dump("S4 after launch notepad")
        qmp.click(w0["x"] + w0["w"] - 20, w0["y"] + TITLE_CY_OFF)
        time.sleep(1.0)
        m.minimize(0)
        dump("S5 after min non-focused files")

        # ---- cleanup ----
        f.cleanup_by_kernel("dbg")
        dump("S6 after cleanup")

        # ---- C2：两次 launch ----
        launch("sysinfo")
        dump("S7 after launch sysinfo")
        launch("notepad")
        dump("S8 after launch notepad")
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
