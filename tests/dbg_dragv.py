# -*- coding: utf-8 -*-
"""dbg_dragv.py - 拖动"掉窗"最小复现：竖向大步进拖动窗口会当场失效

假设（来自 kernel/gfxwin.c gw_handle_mouse 的"拖动中优先命中"分支）：
    容差判据 my ∈ [w->y-8, w->y+GW_TITLE_H+8) = [y-8, y+20)
    拖动**向上**时每次步进 s：my_new = y - s，需要 y - s >= y - 8 → s <= 8。
    Qmp.drag 的步进是 12 → 第一步就掉出容差 → hit 落到别的窗口（或桌面）
    → 拖动态命中不到 → 窗口一动不动，直到释按 gw_end_drags 收场。
    横向不受影响（x 容差 ±16 > 步进 12），向下只在 move_dy>=8 时第二步掉。

本脚本用内核 F12 转储读真实几何（不靠像素、不靠推理）：
    A 向上拖 12 步进  -> 期望跟随；修前预期 y 不变
    B 向下拖 12 步进  -> 期望跟随（对照）
    C 向左拖 12 步进  -> 期望跟随（对照）
    D 向上拖  6 步进  -> 期望跟随（容差内，对照）
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

from probe_combo import Qmp, START_BTN  # noqa: E402
from probe_fuzz import (Fuzzer, FuzzModel, APP_IDX, BOOT_WAIT,  # noqa: E402
                        MENU_ROW_X, MENU_LIST_Y, MENU_ROW_H)
from gui_probe import park_mouse  # noqa: E402
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "dbgdragv_serial.log").replace("\\", "/")
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
        S = [os.path.join(ROOT, "temp", "dbgdragv_s%d.ppm" % i).replace("\\", "/")
             for i in range(4)]
        f = Fuzzer(qmp, m, S, [0], log=LOG)

        def dump(tag):
            hdr, wins = f.gw_state()
            if hdr is None:
                print("%-22s NO DUMP" % tag, flush=True)
                return None
            print("%-22s n=%d wins=%r" % (tag, hdr["n"],
                  [(w["slot"], w["x"], w["y"]) for w in wins]), flush=True)
            return wins

        def menu_open():
            for _ in range(2):
                qmp.click(*START_BTN)
                time.sleep(0.6)
                hdr, _w = f.gw_state()
                if hdr is not None and hdr["menu"] == 1:
                    return True
            return False

        def launch(app):
            if not menu_open():
                print("   !! 菜单没打开")
                return None
            qmp.click(MENU_ROW_X,
                      MENU_LIST_Y + APP_IDX[app] * MENU_ROW_H + MENU_ROW_H // 2)
            time.sleep(1.8)
            return m.launch(app)

        def raw_drag(x0, y0, dx, dy, steps, step=12, press_off=6):
            """按下后手工分步注入相对位移（不走 Qmp.drag 的自动步进）。"""
            qmp.move_to(x0, y0)
            qmp.hmc("mouse_button 1")
            time.sleep(0.12)
            for _ in range(steps):
                sx = max(-step, min(step, dx))
                sy = max(-step, min(step, dy))
                qmp.hmc("mouse_move %d %d" % (sx, sy))
                qmp.cx += sx
                qmp.cy += sy
                time.sleep(0.05)
            time.sleep(0.15)
            qmp.hmc("mouse_button 0")
            time.sleep(0.4)
            park_mouse(qmp)

        def case(tag, dx, dy, steps, step=12):
            wins = dump(tag + " before")
            if not wins:
                print("   !! 没有窗口")
                return
            w = wins[0]
            x0, y0 = w["x"] + 150, w["y"] + 6
            raw_drag(x0, y0, dx, dy, steps, step)
            wins2 = dump(tag + " after ")
            if not wins2:
                return
            w2 = wins2[0]
            exp = (max(0, min(1280 - w2["w"], w["x"] + dx)),
                   max(0, min(992 - w2["h"], w["y"] + dy)))
            got = (w2["x"], w2["y"])
            print("   %-16s 期望%s 实得%s  %s"
                  % (tag, exp, got, "OK" if got == exp else ">>> 掉了"),
                  flush=True)

        park_mouse(qmp)
        launch("sysinfo")
        time.sleep(1.0)
        dump("P0 launched")

        # y=51 的窗口向上拖 96px：ny 会被 desktop_top(0) 钳制，改用
        # 向下开局的窗口。先把窗口拖到桌面中部再逐个 case。
        raw_drag(200, 60, 0, 300, 25, step=12)   # 先向下挪到中部
        dump("P1 moved down")

        case("A 向上 s=12", 0, -96, 8, step=12)
        case("B 向下 s=12", 0, 96, 8, step=12)
        case("C 向左 s=12", -96, 0, 8, step=12)
        case("D 向上 s=6", 0, -96, 16, step=6)
        case("E 向上 s=20", 0, -100, 5, step=20)
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
