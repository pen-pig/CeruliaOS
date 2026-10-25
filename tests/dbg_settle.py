# -*- coding: utf-8 -*-
"""dbg_settle.py - 量化"8 窗全满 + 恢复置顶"后全屏重绘需要多久

A2a（fuzz 42c 唯一 FAIL）：8 窗全满时 menu_row 点 files 菜单行 ->
restore+bring_front -> focus_check 的像素断言 blues=[]（该是 [0]）。
模型与内核对账是通过的，所以不是状态漂移；嫌疑是 screendump 抓到
重绘中间帧。本脚本在同一场景下逐帧测量：每 0.2s 拍一张，直到
(500,57) 出现聚焦蓝，报告需要几帧/几秒。

若确认是重绘慢，则 shot() 的 1.2s settle 在 8 窗满员场景不够，需要
按窗口数动态加大（或改成"等到画面稳定"的轮询）。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

from probe_combo import Qmp, ppm_load, px, START_BTN  # noqa: E402
from probe_fuzz import (Fuzzer, FuzzModel, APP_IDX, BOOT_WAIT,  # noqa: E402
                        MENU_ROW_X, MENU_LIST_Y, MENU_ROW_H)
from gui_probe import park_mouse  # noqa: E402
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "dbgsettle_serial.log").replace("\\", "/")
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
        S = [os.path.join(ROOT, "temp", "dbgsettle_s%d.ppm" % i).replace("\\", "/")
             for i in range(8)]
        f = Fuzzer(qmp, m, S, [0], log=LOG)

        def menu_open():
            for _ in range(2):
                qmp.click(*START_BTN)
                time.sleep(0.6)
                hdr, _w = f.gw_state()
                if hdr is not None and hdr["menu"] == 1:
                    m.menu = True
                    return True
            m.menu = False
            return False

        def menu_row(i):
            qmp.click(MENU_ROW_X, MENU_LIST_Y + i * MENU_ROW_H + MENU_ROW_H // 2)
            time.sleep(1.8)
            m.menu = False

        def launch(app):
            menu_open()
            menu_row(APP_IDX[app])
            return m.launch(app)

        def is_blue(buf):
            return px(buf, 1280, 500, 57)[0] < 5

        park_mouse(qmp)
        # 复刻 A2：按顺序开满 8 个 slot
        order = ["files", "notepad", "sysinfo", "clock", "paint", "terminal",
                 "settings", "about"]
        for a in order:
            launch(a)
        hdr, wins = f.gw_state()
        print("A2 full: n=%d wins=%r" % (hdr["n"], [(w["slot"], w["app"], w["x"], w["y"]) for w in wins]), flush=True)

        # A2a：menu_row(0) 点 files -> restore + bring_front
        menu_open()
        t0 = time.time()
        menu_row(0)
        r = m.launch("files")
        print("A2a launch files -> %s (t=%.2f)" % (r, time.time() - t0), flush=True)
        # 逐帧测量：(500,57) 何时变聚焦蓝
        for i in range(12):
            park_mouse(qmp)
            path = S[i % len(S)]
            qmp.shot(path)
            _w, _h, buf = ppm_load(path)
            b = is_blue(buf)
            print("   frame %2d t=%.2fs (500,57)=%r %s"
                  % (i, time.time() - t0, px(buf, 1280, 500, 57),
                     "<== 聚焦蓝" if b else ""), flush=True)
            if b:
                break
            time.sleep(0.2)
        # 再测一次：直接 F12 触发全屏重绘，测稳定耗时
        print("--- F12 full-redraw settle ---", flush=True)
        t1 = time.time()
        f.gw_state()
        for i in range(12):
            park_mouse(qmp)
            path = S[i % len(S)]
            qmp.shot(path)
            _w, _h, buf = ppm_load(path)
            print("   frame %2d t=%.2fs (500,57)=%r"
                  % (i, time.time() - t1, px(buf, 1280, 500, 57)), flush=True)
            if px(buf, 1280, 500, 57)[0] < 5:
                break
            time.sleep(0.2)
        f.cleanup_by_kernel("dbg")
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
