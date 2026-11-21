# -*- coding: utf-8 -*-
"""dbg_c2c.py - combo2 C2a 的最小复现：拖动后画面没有全屏重绘

已确认的事实链：
  * dbg_c2b（每步 F12 转储后再截图）画面永远正确
  * combo2 run6：C2a（拖后未点击）sysinfo 整窗缺失 -> C2b（点击后）sysinfo 出现
  * 内核转储里几何/焦点全对
=> 差在**重绘时机**：拖动结束后没有触发全屏重绘，画面停在混合帧。

本脚本照抄 combo2 的 C1 -> cleanup -> C2 序列，但**每步之后不转储、直接截图**，
用来区分两种可能：
  (a) launch 之后就没重绘（新窗口没画出来）
  (b) drag 之后就没重绘（窗口被擦掉没重画）
每步额外做一次 F12 转储 + 再拍一帧，直接对比"重绘前/重绘后"的差异。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

from probe_combo import Qmp, START_BTN, TITLE_CY_OFF, ppm_load, px  # noqa: E402
from probe_fuzz import (Fuzzer, FuzzModel, APP_IDX, BOOT_WAIT, TB_Y,  # noqa: E402
                        MENU_ROW_X, MENU_LIST_Y, MENU_ROW_H)
from gui_probe import park_mouse  # noqa: E402
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "dbgc2c_serial.log").replace("\\", "/")
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
        S = [os.path.join(ROOT, "temp", "dbgc2c_s%d.ppm" % i).replace("\\", "/")
             for i in range(6)]
        shots = [0]
        f = Fuzzer(qmp, m, S, shots, log=LOG)

        def dump(tag):
            hdr, wins = f.gw_state()
            if hdr is None:
                print("%-28s NO DUMP" % tag, flush=True)
                return None, []
            print("%-28s menu=%d n=%d wins=%r" %
                  (tag, hdr["menu"], hdr["n"],
                   [(w["slot"], w["app"], w["x"], w["y"], w["hid"], w["foc"],
                     w["max"]) for w in wins]), flush=True)
            return hdr, wins

        def probe(tag, x, y):
            """**先 park 再截图**（对齐 combo2 的 shot()），读一个点；
            再转储 + F12 重绘后再拍一张，读同一个点。"""
            park_mouse(qmp)
            path = S[shots[0] % len(S)]
            shots[0] += 1
            qmp.shot(path)
            w, h, buf = ppm_load(path)
            before = px(buf, w, x, y)
            dump(tag + " [dump]")
            time.sleep(1.5)      # ★ 等全屏重绘完成（16bpp 全屏在 TCG 下很慢）
            path2 = S[shots[0] % len(S)]
            shots[0] += 1
            qmp.shot(path2)
            w2, h2, buf2 = ppm_load(path2)
            after = px(buf2, w2, x, y)
            print("   %-24s (%d,%d) park无F12=%r  F12后=%r  %s"
                  % (tag, x, y, before, after,
                     "SAME" if before == after else ">>> 差异"), flush=True)
            return before, after

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
            if not menu_open():
                print("   !! 菜单没打开")
                return None
            menu_row(APP_IDX[app])
            return m.launch(app)

        park_mouse(qmp)
        dump("P0 park")

        # ================= C1 复刻 =================
        launch("files")
        dump("C1a after launch files")
        w0 = m.slots[0]
        qmp.click(w0["x"] + w0["w"] - 20, w0["y"] + TITLE_CY_OFF)
        time.sleep(1.0)
        m.minimize(0)
        dump("C1a after titlebar-min")

        bx, tw = [(s, b, t) for s, b, t in m.taskbar_buttons()][0][1:]
        qmp.click(bx + tw // 2, TB_Y)
        time.sleep(1.2)
        m.restore(0)
        dump("C1a after taskbar restore")

        launch("notepad")
        dump("C1b after launch notepad")
        qmp.click(w0["x"] + w0["w"] - 20, w0["y"] + TITLE_CY_OFF)
        time.sleep(1.0)
        m.minimize(0)
        dump("C1b after min non-focused files")

        f.cleanup_by_kernel("dbg")
        dump("C1 cleanup")

        # ================= C2 复刻 =================
        launch("sysinfo")
        dump("C2 after launch sysinfo")
        launch("notepad")
        dump("C2 after launch notepad")

        # ★ 拖动前：sysinfo 标题栏中心 (400,57)
        probe("C2 before drag", 400, 57)

        qmp.drag(90, 57, 110, 94)
        time.sleep(0.8)
        # ★ 拖动后：sysinfo 新位置标题栏中心 (420,94)
        probe("C2 after drag", 420, 94)
        # ★ 再等 2s 看是不是"延迟重绘"
        time.sleep(2.0)
        probe("C2 after drag +2s", 420, 94)

        # ★★★ 决定性对照：park_mouse（长距离移动鼠标）之后画面是否被破坏
        path = S[shots[0] % len(S)]
        shots[0] += 1
        w, h, buf = ppm_load(qmp.shot(path))
        print("   %-24s (420,94) park前=%r" % ("park 前", px(buf, w, 420, 94)))
        park_mouse(qmp)
        path = S[shots[0] % len(S)]
        shots[0] += 1
        w, h, buf = ppm_load(qmp.shot(path))
        after_park = px(buf, w, 420, 94)
        print("   %-24s (420,94) park后=%r" % ("park 后", after_park))
        dump("after park [dump]")
        path = S[shots[0] % len(S)]
        shots[0] += 1
        w, h, buf = ppm_load(qmp.shot(path))
        print("   %-24s (420,94) park+F12=%r" % ("park+F12", px(buf, w, 420, 94)))
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
