# -*- coding: utf-8 -*-
"""dbg_click.py - 测量「菜单点击是否被吞」（开始钮 / 菜单行分别统计）

怀疑：GUI 层每帧采样鼠标按键（level），而 press→release 的间隔比一帧
短时，整次点击在主循环眼里就是"按钮始终没按过"——菜单行点不中、窗口
开不出来。探针侧 qmp.click 的按下保持只有 100ms。

这里分两段统计丢失率：
  A) 开始钮（toggle，menu=1/0 可由 F12 转储直接读）
  B) 菜单行（点完应当建出一个 app 窗口）
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
from probe_fuzz import (Fuzzer, FuzzModel, BOOT_WAIT, MENU_ROW_X,  # noqa: E402
                        MENU_LIST_Y, MENU_ROW_H, APP_IDX)
from gui_probe import park_mouse  # noqa: E402
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "dbgclick_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()

ROW = 2                     # sysinfo 行
ROW_Y = MENU_LIST_Y + ROW * MENU_ROW_H + MENU_ROW_H // 2


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
        S = [os.path.join(ROOT, "temp", "dbgclick_s%d.ppm" % i).replace("\\", "/")
             for i in range(4)]
        shots = [0]
        f = Fuzzer(qmp, m, S, shots, log=LOG)

        def st():
            hdr, wins = f.gw_state()
            if hdr is None:
                return None, []
            return hdr["menu"], wins

        def press(hold):
            qmp.hmc("mouse_button 1")
            time.sleep(hold)
            qmp.hmc("mouse_button 0")
            time.sleep(0.35)

        park_mouse(qmp)
        menu, _ = st()
        print("initial menu=%r" % menu, flush=True)

        # ---------- A) 开始钮点击丢失率 ----------
        for hold in (0.1, 0.5):
            miss = 0
            n = 10
            for i in range(n):
                qmp.move_to(*START_BTN)          # 鼠标就位（此后不动）
                press(hold)
                time.sleep(0.3)
                menu, _ = st()
                if menu != 1:
                    miss += 1
                    print("   open  hold=%.2f #%d MISS menu=%r" % (hold, i, menu),
                          flush=True)
                else:
                    qmp.move_to(*START_BTN)
                    press(hold)                   # 关掉
                    time.sleep(0.3)
                    menu2, _ = st()
                    if menu2 != 0:
                        print("   close hold=%.2f #%d MISS menu=%r" % (hold, i, menu2),
                              flush=True)
                        qmp.move_to(*START_BTN)
                        press(0.5)
                        time.sleep(0.4)
            print("A) hold=%.2fs : %d/%d 打开丢失" % (hold, miss, n), flush=True)

        # ---------- B) 菜单行点击丢失率 ----------
        for hold in (0.1, 0.5):
            miss = 0
            n = 6
            for i in range(n):
                # 先确保菜单开着
                qmp.move_to(*START_BTN)
                press(0.4)
                time.sleep(0.3)
                menu, _ = st()
                if menu != 1:
                    qmp.move_to(*START_BTN)
                    press(0.4)
                    time.sleep(0.4)
                    menu, _ = st()
                if menu != 1:
                    print("   B #%d 菜单开不了，跳过" % i, flush=True)
                    continue
                qmp.click(MENU_ROW_X, ROW_Y)
                time.sleep(1.2)
                menu, wins = st()
                want = APP_IDX["sysinfo"]
                got = [w for w in wins if w["app"] == want]
                if not got:
                    miss += 1
                    print("   row   hold=%.2f #%d MISS wins=%r menu=%r" %
                          (hold, i, wins, menu), flush=True)
                    # 清掉可能建出来的窗口
                    for w in wins:
                        qmp.click(w["x"] + w["w"] - 7, w["y"] + 6)
                        time.sleep(0.8)
            print("B) hold=%.2fs : %d/%d 菜单行丢失" % (hold, miss, n), flush=True)
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
