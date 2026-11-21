# -*- coding: utf-8 -*-
"""dbg_redraw.py - 量化 16bpp 全屏重绘耗时（screendump 抓中间帧问题）

事实链（2026-10-24）：
  combo2 C2a：拖动后立刻截图 -> sysinfo 整窗缺失、桌面图标露出来
  dbg_c2c：F12 后 sleep 1.5s 再截图 -> 画面完全正常
=> screendump 会抓到"全屏重绘进行中"的半成品帧（16bpp 1280x1024 = 260 万像素，
   QEMU TCG 下单帧很慢）。这不是内核 bug，是探针截图时机缺陷。

本脚本量化：从 F12 触发全屏重绘开始，每 0.25s 拍一帧，记录画面何时稳定。
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
from probe_fuzz import (Fuzzer, FuzzModel, APP_IDX, BOOT_WAIT,  # noqa: E402
                        MENU_ROW_X, MENU_LIST_Y, MENU_ROW_H)
from gui_probe import park_mouse  # noqa: E402
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "dbgredraw_serial.log").replace("\\", "/")
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
        S = [os.path.join(ROOT, "temp", "dbgredraw_s%d.ppm" % i).replace("\\", "/")
             for i in range(10)]
        shots = [0]
        f = Fuzzer(qmp, m, S, shots, log=LOG)

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
                return
            qmp.click(MENU_ROW_X, MENU_LIST_Y + APP_IDX[app] * MENU_ROW_H
                      + MENU_ROW_H // 2)
            time.sleep(1.8)
            m.menu = False
            m.launch(app)

        park_mouse(qmp)
        launch("sysinfo")
        launch("notepad")
        hdr, wins = f.gw_state()
        print("state: %r" % ([(w["slot"], w["app"], w["x"], w["y"], w["foc"])
                              for w in wins],), flush=True)

        # ★ 触发一次全屏重绘（F12 -> gw_dirty=1 -> gw_draw_all）
        # 然后每 0.25s 拍一帧，记录 sysinfo 标题栏中心 (420,94) 的颜色
        qmp.hmc("sendkey f12")
        t0 = time.time()
        prev = None
        for i in range(10):
            time.sleep(0.25)
            path = S[shots[0] % len(S)]
            shots[0] += 1
            qmp.hmc("screendump " + path)
            time.sleep(0.15)
            w, h, buf = ppm_load(path)
            c = px(buf, w, 420, 94)
            n_np = px(buf, w, 496, 103)
            same = "-" if prev is None else ("same" if prev == buf else "DIFF")
            print("  t=+%.2fs  (%d,%d)=%r  notepad_t=%r  %s"
                  % (time.time() - t0, 420, 94, c, n_np, same), flush=True)
            prev = buf
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
