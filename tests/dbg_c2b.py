# -*- coding: utf-8 -*-
"""dbg_c2b.py - 定位 combo2 C2a 的"sysinfo 窗口没被画出来"

现象（combo2_run6.log）：
  C2 end 模型-内核对账 PASS -> 内核里 sysinfo 确实在 (80,88)
  但 C2a 截图里 (420,94) 是桌面图标绿 (0,207,66)，(496,103) 是灰 (99,101,99)
  => 画面上 **sysinfo 标题栏整条没画**，notepad 标题栏却是灰（非聚焦态）

这脚本做 A/B 对照，逐步回答"sysinfo 从哪一步起不画了"：
  A. 只 launch sysinfo          -> 期望 (400,57) 是标题栏
  B. 再 launch notepad           -> 看 sysinfo 标题栏是否还在
  C. drag sysinfo 标题栏         -> 看新位置 (420,94) 是否出现标题栏
每步都 F12 转储拿内核真值 + 截图 + 像素分类图。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

from probe_combo import Qmp, START_BTN, ppm_load, px  # noqa: E402
from probe_fuzz import (Fuzzer, FuzzModel, APP_IDX, BOOT_WAIT, TB_Y,  # noqa: E402
                        MENU_ROW_X, MENU_LIST_Y, MENU_ROW_H)
from gui_probe import park_mouse  # noqa: E402
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "dbgc2b_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()


def rm(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
    except Exception:   # 沙箱 safe-delete 抛的非 OSError 也要吞
        pass


def cls(c):
    r, g, b = c
    if r < 8 and 190 <= g <= 220 and 50 <= b <= 80:
        return "G"          # accent 绿 / Terminal 图标
    if b > 140 and 90 <= g <= 140 and r < 90:
        return "B"          # 标题栏蓝（聚焦）
    if abs(r - g) < 14 and abs(g - b) < 14 and 60 <= r <= 140:
        return "k"          # 标题栏灰（非聚焦）
    if r > 235 and g > 235 and b > 235:
        return "W"          # 窗口内容白
    if b > 150 and r < 120:
        return "~"          # 桌面蓝
    if r > 200 and g > 200 and b > 200:
        return "w"          # 浅内容底 #F3F3F3
    return "?"


def classify(buf, w, x0, x1, y0, y1, step=4):
    print("      x:%s" % "".join(str((x // 10) % 10) for x in range(x0, x1, 10)))
    for y in range(y0, y1, 2):
        print("  %4d %s" % (y, "".join(cls(px(buf, w, x, y))
                                       for x in range(x0, x1, step))))


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
        S = [os.path.join(ROOT, "temp", "dbgc2b_s%d.ppm" % i).replace("\\", "/")
             for i in range(4)]
        shots = [0]
        f = Fuzzer(qmp, m, S, shots, log=LOG)

        def dump(tag):
            hdr, wins = f.gw_state()
            if hdr is None:
                print("%-30s NO DUMP" % tag, flush=True)
                return None, []
            print("%-30s menu=%d n=%d wins=%r" %
                  (tag, hdr["menu"], hdr["n"],
                   [(w["slot"], w["app"], w["x"], w["y"], w["hid"], w["foc"],
                     w["max"]) for w in wins]), flush=True)
            return hdr, wins

        def shot(tag):
            path = S[shots[0] % len(S)]
            shots[0] += 1
            qmp.shot(path)
            w, h, buf = ppm_load(path)
            return w, h, buf

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

        def launch(app):
            if not menu_open():
                print("   !! 菜单没打开")
                return None
            qmp.click(MENU_ROW_X, MENU_LIST_Y + APP_IDX[app] * MENU_ROW_H
                      + MENU_ROW_H // 2)
            time.sleep(1.8)
            m.menu = False
            return m.launch(app)

        park_mouse(qmp)
        dump("A0 park")

        # ---- A: 只 launch sysinfo ----
        launch("sysinfo")
        hdr, wins = dump("A1 after launch sysinfo")
        w, h, buf = shot("A1")
        print("   A1 sysinfo title probe (400,57)=%r  (420,94)=%r  (300,57)=%r"
              % (px(buf, w, 400, 57), px(buf, w, 420, 94), px(buf, w, 300, 57)))
        classify(buf, w, 60, 780, 44, 112)

        # ---- B: 再 launch notepad ----
        launch("notepad")
        hdr, wins = dump("B1 after launch notepad")
        w, h, buf = shot("B1")
        print("   B1 sysinfo title probe (400,57)=%r  (420,94)=%r  (496,103)=%r"
              % (px(buf, w, 400, 57), px(buf, w, 420, 94), px(buf, w, 496, 103)))
        classify(buf, w, 60, 780, 44, 112)

        # ---- C: 拖 sysinfo 标题栏 (90,57) -> (110,94) ----
        qmp.drag(90, 57, 110, 94)
        time.sleep(1.0)
        hdr, wins = dump("C1 after drag")
        w, h, buf = shot("C1")
        print("   C1 new pos probe (420,94)=%r  (200,94)=%r  (496,103)=%r"
              % (px(buf, w, 420, 94), px(buf, w, 200, 94), px(buf, w, 496, 103)))
        classify(buf, w, 60, 780, 44, 112)
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
