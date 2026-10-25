# -*- coding: utf-8 -*-
"""dbg_menu.py - 定位「开始菜单启动 app 不出窗口」的根因（机器判定）

现象（2026-10-24 combo2_final）：C2 起内核窗口全空，探针模型认为窗口
已经开好；C5/C6/C7 里又时好时坏。像素上「窗口没开出来」和「位置算错」
分不开，所以这里逐步 F12 转储，把「指针到没到」「菜单开没开」「窗建没建」
三件事分开量。

关键测量点：
  d0  停车后：内核指针位置（基线）
  d1  长距离 move_to 之后**立刻**转储：内核指针落后多少
  d2  等 3s 再转储：内核追上没有（追不上 = 事件被丢，不是慢）
  d3  在已经在目标位置时按开始钮：菜单开没开
  d4/d5 点菜单行后：窗口建没建
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
from probe_fuzz import Fuzzer, FuzzModel, APP_IDX, BOOT_WAIT  # noqa: E402
from gui_probe import park_mouse  # noqa: E402
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "dbgmenu_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()

MENU_ROW_X = 248
MENU_LIST_Y = 130
MENU_ROW_H = 61


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
        S = [os.path.join(ROOT, "temp", "dbgmenu_s%d.ppm" % i).replace("\\", "/")
             for i in range(4)]
        shots = [0]
        f = Fuzzer(qmp, m, S, shots, log=LOG)

        def dump(tag):
            hdr, wins = f.gw_state()
            if hdr is None:
                print("%-28s NO DUMP" % tag, flush=True)
                return None, []
            print("%-28s kernel mx=(%d,%d) menu=%d n=%d wins=%r  probe=(%d,%d)" %
                  (tag, hdr["mx"], hdr["my"], hdr["menu"], hdr["n"],
                   [(w["slot"], w["app"], w["x"], w["y"], w["hid"], w["foc"])
                    for w in wins], qmp.cx, qmp.cy), flush=True)
            return hdr, wins

        park_mouse(qmp)
        dump("d0 park baseline")

        # 长距离移动：1200 -> 24（98 步 x 12px），内核若丢事件这里看得出
        t0 = time.time()
        qmp.move_to(START_BTN[0], START_BTN[1])
        t1 = time.time()
        print("move_to(24,1008) took %.2fs (probe)" % (t1 - t0), flush=True)
        dump("d1 right after move")
        time.sleep(3.0)
        dump("d2 after 3s settle")

        # 指针已经在开始钮上：按下去看菜单开不开
        qmp.click(*START_BTN)
        time.sleep(0.8)
        dump("d3 after START_BTN click")

        # 点菜单第 2 行（sysinfo）
        qmp.click(MENU_ROW_X, MENU_LIST_Y + 2 * MENU_ROW_H + MENU_ROW_H // 2)
        time.sleep(1.0)
        dump("d4 +1s after row2 click")
        time.sleep(2.0)
        dump("d5 +3s after row2 click")
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
