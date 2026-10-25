# -*- coding: utf-8 -*-
"""dbg_a2.py - A2「8 slot 打满」场景的逐次 launch 转储（定位 flaky 点）

A2 场景偶发红（2026-10-24 两次运行一次红一次绿）：8 个 app 依次从开始
菜单启动后，"只有 slot7 是蓝标题"断言读不到任何蓝标题。像素分不清是
"窗口没开出来"还是"位置算错"，所以这里**每启动一个 app 就 F12 转储**，
逐步打印内核的窗口列表与焦点槽位，机器判定是谁错。
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
from probe_fuzz import (FuzzModel, Fuzzer, APP_IDX, BOOT_WAIT, TB_Y)  # noqa: E402
from probe_chain import title_blue_exact  # noqa: E402
from gui_probe import mouse_sync  # noqa: E402
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "dbga2_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()

APPS = ["files", "notepad", "sysinfo", "clock", "about",
        "terminal", "paint", "settings"]


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
        S = [os.path.join(ROOT, "temp", "dbga2_s%d.ppm" % i).replace("\\", "/")
             for i in range(4)]
        shots = [0]
        f = Fuzzer(qmp, m, S, shots, log=LOG)

        for app in APPS:
            idx = APP_IDX[app]
            qmp.click(*START_BTN)
            time.sleep(0.6)
            qmp.click(248, 130 + idx * 61 + 30)
            time.sleep(1.8)
            m.menu = False
            m.launch(app)
            hdr, wins = f.gw_state()
            klist = [(w["slot"], w["app"], w["x"], w["y"], w["hid"], w["foc"])
                     for w in wins]
            mlist = [(s, APP_IDX[m.slots[s]["app"]], m.slots[s]["x"],
                      m.slots[s]["y"], 1 if m.slots[s]["hidden"] else 0,
                      1 if s == m.focused else 0) for s in m.used()]
            sync, det = mouse_sync(hdr, qmp)
            print("launch %-9s -> kernel n=%s focus=%s" %
                  (app, hdr["n"] if hdr else None,
                   hdr["focus"] if hdr else None), flush=True)
            print("    kernel=%r" % (klist,), flush=True)
            print("    model =%r" % (mlist,), flush=True)
            print("    match=%s  %s" % (klist == mlist, det), flush=True)

        # 像素判定：聚焦窗标题中心必须是蓝
        hdr, wins = f.gw_state()
        if not wins:
            print("FAIL: no windows")
            return 1
        foc = [w for w in wins if w["foc"]]
        if not foc:
            print("FAIL: no focused window")
            return 1
        fw = foc[0]
        tcx, tcy = fw["x"] + fw["w"] // 2, fw["y"] + TITLE_CY_OFF
        f.shot(30)
        _, _, buf = ppm_load(S[30 % len(S)])
        c = px(buf, 1280, tcx, tcy)
        print("%s 聚焦窗标题是蓝 | slot=%d (%d,%d)=%r" %
              ("PASS" if title_blue_exact(c) else "FAIL", fw["slot"], tcx, tcy, c),
              flush=True)
        return 0 if title_blue_exact(c) else 1
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
