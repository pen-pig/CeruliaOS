# -*- coding: utf-8 -*-
"""dbg_m6diag.py - 复刻 dbg_mut M6 段，打印匹配明细定位全不匹配的原因"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

from probe_combo import Qmp, ppm_load, px, START_BTN  # noqa: E402
from probe_fuzz import (Fuzzer, FuzzModel, BOOT_WAIT,  # noqa: E402
                        MENU_ROW_X, MENU_LIST_Y, MENU_ROW_H, APP_IDX)
from gui_probe import park_mouse  # noqa: E402
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "dbgm6_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()


def rm(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
    except Exception:
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
        S = [os.path.join(ROOT, "temp", "m6_s%d.ppm" % i).replace("\\", "/")
             for i in range(22)]
        shots = [0]
        f = Fuzzer(qmp, m, S, shots, log=LOG)
        park_mouse(qmp)
        print("after park: cx,cy=%r" % ((qmp.cx, qmp.cy),), flush=True)

        # launch files（照抄 dbg_mut）
        for _ in range(2):
            qmp.click(*START_BTN)
            time.sleep(0.6)
            hdr, _w = f.gw_state()
            if hdr is not None and hdr["menu"] == 1:
                m.menu = True
                break
        qmp.click(MENU_ROW_X, MENU_LIST_Y + APP_IDX["files"] * MENU_ROW_H
                  + MENU_ROW_H // 2)
        time.sleep(1.6)
        m.menu = False
        m.launch("files")

        h6, wins6 = f.gw_state()
        print("state: %r" % ([(w["slot"], w["app"], w["x"], w["y"], w["w"], w["h"])
                              for w in wins6],), flush=True)
        g6 = wins6[0]

        IN_X, IN_Y = g6["x"] + 2, g6["y"] + 16
        IN_W, IN_H = g6["w"] - 4, g6["h"] - 20
        print("IN_X=%d IN_Y=%d IN_W=%d IN_H=%d" % (IN_X, IN_Y, IN_W, IN_H), flush=True)

        rp = S[shots[0] % len(S)]
        shots[0] += 1
        qmp.shot(rp)
        _rw, _rh, rbuf = ppm_load(rp)
        print("R0: %dx%d  title-center=%r  content-center=%r"
              % (_rw, _rh, px(rbuf, _rw, g6["x"] + 200, g6["y"] + 6),
                 px(rbuf, _rw, g6["x"] + 400, g6["y"] + 300)), flush=True)

        qmp.move_to(g6["x"] + 100, g6["y"] + 2)
        time.sleep(0.25)
        print("before press: cx,cy=%r" % ((qmp.cx, qmp.cy),), flush=True)
        qmp.hmc("mouse_button 1")
        time.sleep(0.15)

        for i in range(3):
            qmp.hmc("mouse_move 0 6")
            qmp.cy += 6
            time.sleep(0.28)
            p = S[shots[0] % len(S)]
            shots[0] += 1
            qmp.shot(p)
            w, h, buf = ppm_load(p)
            wy = g6["y"] + (i + 1) * 6
            print("frame %d: %dx%d title-center=%r content-center=%r (expect y=%d)"
                  % (i, w, h, px(buf, w, g6["x"] + 200, wy + 6),
                     px(buf, w, g6["x"] + 400, wy + 300), wy), flush=True)
            # 采样点明细
            n = 0
            for yy in range(0, 40, 3):
                ro = ((IN_Y + yy) * _rw + IN_X) * 3
                bo = ((wy + 16 + yy) * w + IN_X) * 3
                for xx in range(0, 40, 7):
                    cr = tuple(rbuf[ro + xx * 3:ro + xx * 3 + 3])
                    cb = tuple(buf[bo + xx * 3:bo + xx * 3 + 3])
                    print("   (%3d,%3d) R0=%r F=%r %s"
                          % (IN_X + xx, IN_Y + yy, cr, cb,
                             "=" if cr == cb else "!="), flush=True)
                    n += 1
                    if n >= 8:
                        break
                if n >= 8:
                    break
        qmp.hmc("mouse_button 0")
        time.sleep(0.4)
        return 0
    finally:
        try:
            qmp.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
