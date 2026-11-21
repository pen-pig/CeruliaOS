# -*- coding: utf-8 -*-
"""dbg_c9.py - 标题栏按钮"连点"行为的快速定向验证（机器判定）

场景（probe_combo2 的 C9）：
  1) 开一个窗口
  2) 连点两次标题栏最小化按钮（间隔 60ms）
  3) 内核转储必须仍然报 hid=1（仍最小化）

缺陷（修复前）：第二次按下时窗口已隐藏 -> 同一坐标变成"桌面空白"
-> 走桌面分支 -> "恢复 slot 最小的隐藏窗" -> 刚最小化的窗口被立刻恢复
（连点最小化等于没最小化）。判据用内核转储，不靠像素。

同时验证"点桌面恢复最小化窗"这条正常语义没被连点保护误伤：
  4) 用**任务栏**最小化（不 arm 连点保护）-> 点桌面空白 -> 必须恢复
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
from probe_fuzz import (FuzzModel, Fuzzer, APP_IDX, BOOT_WAIT, TB_Y)  # noqa: E402
from gui_probe import mouse_sync  # noqa: E402
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "dbgc9_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()

MIN_DX = -20
CLOSE_DX = -7


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
    rc = 0
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
        S = [os.path.join(ROOT, "temp", "dbgc9_s%d.ppm" % i).replace("\\", "/")
             for i in range(4)]
        shots = [0]
        f = Fuzzer(qmp, m, S, shots, log=LOG)

        def launch(app):
            qmp.click(*START_BTN)
            time.sleep(0.6)
            qmp.click(248, 130 + APP_IDX[app] * 61 + 30)
            time.sleep(1.8)
            m.menu = False
            return m.launch(app)

        def state():
            hdr, wins = f.gw_state()
            ok2, det = mouse_sync(hdr, qmp)
            print("  kernel: n=%r focus=%r wins=%r" %
                  (hdr["n"] if hdr else None,
                   hdr["focus"] if hdr else None,
                   [(w["slot"], w["app"], w["hid"], w["foc"]) for w in wins]),
                  flush=True)
            print("  pointer: %s" % det, flush=True)
            return hdr, wins

        # --- 1) 开窗 ---
        launch("files")
        hdr, wins = state()
        if not wins:
            print("FAIL: window not created")
            return 1
        w0 = wins[0]
        minx, miny = w0["x"] + w0["w"] + MIN_DX, w0["y"] + TITLE_CY_OFF

        # --- 2) 连点两次最小化按钮 ---
        qmp.move_to(minx, miny)
        for _ in range(2):
            qmp.hmc("mouse_button 1")
            time.sleep(0.06)
            qmp.hmc("mouse_button 0")
            time.sleep(0.06)
        time.sleep(1.2)
        hdr, wins = state()
        hid = wins[0]["hid"] if wins else None
        ok2 = hid == 1
        print("%s C9: 连点标题栏最小化钮后窗口仍最小化 | hid=%r (期望 1)" %
              ("PASS" if ok2 else "FAIL", hid), flush=True)
        if not ok2:
            rc = 1

        # --- 3) 正常语义没被误伤：任务栏最小化 -> 点桌面空白 -> 恢复 ---
        # 先把上面的窗口恢复回来（任务栏按钮），再走一遍任务栏最小化路径
        if wins and wins[0]["tbx"] >= 0:
            qmp.click(wins[0]["tbx"] + wins[0]["tw"] // 2, TB_Y)
            time.sleep(1.0)
            hdr, wins = state()
            if wins and wins[0]["tbx"] >= 0:
                qmp.click(wins[0]["tbx"] + wins[0]["tw"] // 2, TB_Y)
                time.sleep(1.0)          # 任务栏最小化
                hdr, wins = state()
                hid2 = wins[0]["hid"] if wins else None
                qmp.click(1000, 400)      # 点桌面空白 -> 应恢复 slot 最小的隐藏窗
                time.sleep(1.2)
                hdr2, wins2 = state()
                hid3 = wins2[0]["hid"] if wins2 else None
                ok3 = hid2 == 1 and hid3 == 0
                print("%s 桌面空白恢复最小化窗（连点保护未误伤）| "
                      "min后hid=%r 点桌面后hid=%r (期望 1 -> 0)" %
                      ("PASS" if ok3 else "FAIL", hid2, hid3), flush=True)
                if not ok3:
                    rc = 1
        return rc
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
