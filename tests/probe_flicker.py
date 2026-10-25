# -*- coding: utf-8 -*-
"""probe_flicker.py - 量化"拖动闪动"：逐帧内容匹配率 + 帧间 diff 峰值

背景（2026-10-25 用户反馈"闪动还是太严重"）：
  1) gw_redraw_region() 第一句是无条件 gw_draw_desktop()（全屏壁纸+水印+
     Dock+13 图标）——拖动时每帧把整屏擦一遍再画窗口。
  2) 即便桌面已局部化，合并区（旧位置∪新位置）仍包含**整个窗口**，整窗
     内容全量重画 ≈ 1.1M 像素/帧。QEMU TCG 下单帧 200-400ms，超过鼠标包
     间隔（真机 PS/2 100Hz 只有 10ms）——screendump 与肉眼持续看到"桌面
     已擦、窗口还没画完"的半成品帧。
  修法：显存内 32 位块搬移整个窗口矩形（内容相对窗口不变），只重画旧
  位置露出的 |d| 像素窄带；另把 SysInfo 的每秒全屏重绘改为窗口局部。

机器判据（不靠肉眼）：
  A. 逐帧内容匹配率：与拖动前参考帧同区域逐点比。半成品帧会掉到 0.9
     以下（实测最低 0.19），正常帧 0.95+。
  B. 帧间 diff 峰值：正常 ~2.3 万（移动带+内容双位置），半成品帧 ~47 万
     （差 20 倍）。
  C. 丢包检测：最终窗口位置 == 注入总量（证明 24 个鼠标包全部被处理，
     匹配率低不是"窗口没动"造成的假象）。

退出码：全部判据过 = 0，任一不过 = 1。
"""
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
                        MENU_ROW_X, MENU_LIST_Y, MENU_ROW_H)
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "flk_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()
NSHOT = 26          # 截图槽位数（沙箱单命令文件操作阈值 ~50，循环复用）
STEP = 6            # 单步 6px：模拟平滑拖动
V_STEPS = 20        # 竖向段步数
H_STEPS = 16        # 横向段步数
MATCH_MIN = 0.90    # 内容匹配率下限（正常 0.95+，半成品帧 <0.90）
DIFF_PEAK_MAX = 100000   # 帧间 diff 峰值上限（正常 ~2.3 万，半成品 ~47 万）


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
        S = [os.path.join(ROOT, "temp", "flk_s%d.ppm" % i).replace("\\", "/")
             for i in range(NSHOT)]
        shots = [0]
        f = Fuzzer(qmp, m, S, shots, log=LOG)

        # ---- 开一个 files 窗口（不转储：F12 会触发全屏重绘，掩盖问题） ----
        qmp.click(*START_BTN)
        time.sleep(0.8)
        qmp.click(MENU_ROW_X, MENU_LIST_Y + 0 * MENU_ROW_H + MENU_ROW_H // 2)
        time.sleep(2.5)
        hdr, wins = f.gw_state()
        if not wins:
            print("LAUNCH FAILED")
            return 1
        g = wins[0]
        print("window: slot=%d app=%d (%d,%d) %dx%d" %
              (g["slot"], g["app"], g["x"], g["y"], g["w"], g["h"]), flush=True)
        # 内容区（避开边框/标题栏/工具栏）：files 内容随窗口整体平移，
        # 与 R0 同区域逐像素应完全相同——掉下去的就是"没画完的半成品帧"
        IN_X, IN_Y = g["x"] + 2, g["y"] + 16
        IN_W, IN_H = g["w"] - 4, g["h"] - 20

        # ---- 参考帧 R0（窗口在初始位，内容完整；此后一直驻留内存） ----
        rpath = S[shots[0] % NSHOT]
        shots[0] += 1
        qmp.shot(rpath)
        rw, rh, rbuf = ppm_load(rpath)

        all_ok = True

        def drag_round(dx, dy, n, tag):
            """一段小步进拖动（dx/dy 只能是 (0,1) 或 (1,0)）。
            返回 (匹配率列表, diff 峰值, 丢包数)。"""
            nonlocal all_ok
            # 每段前用转储拿**当前**窗口位置（内核真值，不靠模型推算）
            hg, wg = f.gw_state()
            if not wg:
                print("%s: NO WINDOW" % tag)
                all_ok = False
                return [], 0, 0
            gg = wg[0]
            sx, sy = gg["x"] + 100, gg["y"] + 2
            qmp.move_to(sx, sy)
            time.sleep(0.25)
            qmp.hmc("mouse_button 1")
            time.sleep(0.15)

            ratios, peak, prev = [], 0, None
            for i in range(n):
                # QMP mouse_move 是**相对增量**（内核 PS/2 是相对设备）
                qmp.hmc("mouse_move %d %d" % (dx * STEP, dy * STEP))
                qmp.cx += dx * STEP
                qmp.cy += dy * STEP
                time.sleep(0.28)      # 给内核一帧时间处理 + 搬移
                p = S[shots[0] % NSHOT]
                shots[0] += 1
                qmp.shot(p)
                w, h, buf = ppm_load(p)
                wx = gg["x"] + (i + 1) * STEP * dx
                wy = gg["y"] + (i + 1) * STEP * dy
                same = tot = 0
                for yy in range(0, IN_H, 3):
                    ro = ((IN_Y + yy) * rw + IN_X) * 3
                    bo = ((wy + 16 + yy) * w + wx + 2) * 3
                    for xx in range(0, IN_W, 7):
                        tot += 1
                        if rbuf[ro + xx * 3:ro + xx * 3 + 3] == \
                                buf[bo + xx * 3:bo + xx * 3 + 3]:
                            same += 1
                ratios.append(same / tot if tot else 0.0)
                if prev is not None:
                    d = 0
                    for k in range(0, min(len(prev), len(buf)), 48):
                        if prev[k] != buf[k]:
                            d += 16
                    if d > peak:
                        peak = d
                prev = buf
            qmp.hmc("mouse_button 0")
            time.sleep(0.5)

            # 判据 C：丢包检测（最终位置 vs 注入总量）
            hg2, wg2 = f.gw_state()
            lost = 0
            if wg2:
                got = (wg2[0]["x"], wg2[0]["y"])
                want = (gg["x"] + n * STEP * dx, gg["y"] + n * STEP * dy)
                lost = abs(got[0] - want[0]) + abs(got[1] - want[1])
                print("%s: final=%r want=%r %s"
                      % (tag, got, want, "OK" if lost == 0 else ">>> 丢包 %d" % lost),
                      flush=True)

            worst = min(ratios) if ratios else 0.0
            print("%s: frames=%d worst-match=%.2f diff-peak=%d"
                  % (tag, len(ratios), worst, peak), flush=True)
            ok_m = worst >= MATCH_MIN
            ok_d = peak <= DIFF_PEAK_MAX
            ok_l = lost == 0
            print("%s: match %s (>=%.2f) | diff %s (<=%d) | packets %s"
                  % (tag, "PASS" if ok_m else "FAIL", MATCH_MIN,
                     "PASS" if ok_d else "FAIL", DIFF_PEAK_MAX,
                     "PASS" if ok_l else "FAIL"), flush=True)
            if not (ok_m and ok_d and ok_l):
                all_ok = False
                print("   ratios=%r" % (["%.2f" % r for r in ratios],), flush=True)
            return ratios, peak, lost

        drag_round(0, 1, V_STEPS, "vertical")
        drag_round(1, 0, H_STEPS, "horizontal")

        print("\n===== probe_flicker: %s =====" % ("ALL PASS" if all_ok else "HAS FAILURES"))
        return 0 if all_ok else 1
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
