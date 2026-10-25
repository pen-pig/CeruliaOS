# -*- coding: utf-8 -*-
"""dbg_dclick.py - 定位"上拖时窗口被最大化"的假双击

现象（tests/dbg_mut.py M5）：down-drag(step=24) 成功把 files 拖到 y=351，
紧接着 up-drag(step=24) 后窗口变成 (0,0,1280,992) max=1 —— 即拖动中途
被 gw_toggle_max 了。toggle_max 只在 gw_handle_mouse 的 pressed 分支调用，
判据是 "同一窗口标题栏 400ms 内第二次按下"。

疑点：两次 press 之间探针 sleep 了约 2.5s，按 1000Hz 的 g_pit_ticks 算是
2500 tick，不该 < 400。唯一自洽的解释是**内核忙（每个鼠标事件都置
gw_dirty -> 1280x1024 16bpp 全屏重绘，TCG 下极慢）时 PS/2 事件排队/溢出，
down-drag 的 press 被延后处理**，与 up-drag 的 press 在 tick 时间上挨到
一起 -> 假双击。

本脚本用时间变体验证：若"两次拖拽之间多等几秒"就不再最大化，即证实是
事件排队（慢系统）导致的假双击，而不是判据本身错。
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
LOG = os.path.join(ROOT, "temp", "dbgdclick_serial.log").replace("\\", "/")
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
        S = [os.path.join(ROOT, "temp", "dbgdclick_s%d.ppm" % i).replace("\\", "/")
             for i in range(2)]
        f = Fuzzer(qmp, m, S, [0], log=LOG)

        def state(tag):
            hdr, wins = f.gw_state()
            if not wins:
                print("%-26s n=0" % tag, flush=True)
                return None
            w = wins[0]
            print("%-26s x=%d y=%d w=%d h=%d max=%d"
                  % (tag, w["x"], w["y"], w["w"], w["h"], w["max"]), flush=True)
            return w

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
                print("!! menu open failed")
                return
            qmp.click(MENU_ROW_X,
                      MENU_LIST_Y + APP_IDX[app] * MENU_ROW_H + MENU_ROW_H // 2)
            time.sleep(1.8)
            m.menu = False
            m.launch(app)

        def down_up(tag, extra_sleep=0.0, use_state=True, park=False):
            launch("files")
            g = state(tag + " launched")
            if not g:
                return
            x0, y0 = g["x"] + 150, g["y"] + 2
            qmp.drag(x0, y0, x0, y0 + 300, step=24)
            time.sleep(1.0)
            g1 = state(tag + " after down")
            if not g1:
                return
            if use_state:
                f.gw_state()
            if extra_sleep:
                time.sleep(extra_sleep)
            if park:
                park_mouse(qmp)
            xa, ya = g1["x"] + 150, g1["y"] + 2
            qmp.drag(xa, ya, xa, ya - 300, step=24)
            time.sleep(1.0)
            g2 = state(tag + " after up  ")
            if g2:
                print("   => %s %s" % (tag,
                      "OK(窗口跟手)" if (g2["x"], g2["y"]) == (g1["x"], g1["y"] - 300)
                      else ">>> 被最大化/异常"), flush=True)
            f.cleanup_by_kernel("cleanup")

        park_mouse(qmp)
        down_up("V1 间隔0+F12", extra_sleep=0.0, use_state=True, park=False)
        down_up("V2 间隔3s+F12", extra_sleep=3.0, use_state=True, park=False)
        down_up("V3 间隔3s+park", extra_sleep=3.0, use_state=True, park=True)
        down_up("V4 无F12", extra_sleep=0.0, use_state=False, park=False)
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
