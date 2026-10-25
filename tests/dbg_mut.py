# -*- coding: utf-8 -*-
"""dbg_mut.py - 变异测试用的**小而准**场景探针（每条断言对应一个变异）

一次跑完三个可变异点，3 分钟内给出 PASS/FAIL，供"改内核 -> 断言必须变红"
的变异测试用：

  M1  标题栏最小化钮失效          -> gw_title_btn_hit 关掉 btn==1 分支
  M2  三连击连触发（max 切换两次）-> 去掉 last_title_wnd = NULL
  M3  cascade y 步进 46 -> 44      -> gw_spawn_app 的 gw_scale_y(22)

判据全部是**内核转储**（机器判定，不靠读码推理、不靠像素猜）。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

from probe_combo import (Qmp, ppm_load, px, TITLE_CY_OFF,  # noqa: E402
                         START_BTN)
from probe_fuzz import (Fuzzer, FuzzModel, APP_IDX, BOOT_WAIT, TB_Y,  # noqa: E402
                        MENU_ROW_X, MENU_LIST_Y, MENU_ROW_H)
from gui_probe import park_mouse  # noqa: E402
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "dbgmut_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()

RESULTS = []


def rep(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       (" | " + detail) if detail else ""), flush=True)


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
        S = [os.path.join(ROOT, "temp", "dbgmut_s%d.ppm" % i).replace("\\", "/")
             for i in range(4)]
        shots = [0]
        f = Fuzzer(qmp, m, S, shots, log=LOG)

        def state():
            return f.gw_state()

        def menu_open():
            for _ in range(2):
                qmp.click(*START_BTN)
                time.sleep(0.6)
                hdr, _w = state()
                if hdr is not None and hdr["menu"] == 1:
                    m.menu = True
                    return True
            m.menu = False
            return False

        def launch(app):
            menu_open()
            qmp.click(MENU_ROW_X, MENU_LIST_Y + APP_IDX[app] * MENU_ROW_H
                      + MENU_ROW_H // 2)
            time.sleep(1.6)
            m.menu = False
            return m.launch(app)

        def cleanup():
            f.cleanup_by_kernel("mut")

        park_mouse(qmp)

        # ---- M1: 标题栏最小化钮必须把窗口藏起来（hid=1, 焦点置 NULL）----
        launch("files")
        w0 = m.slots[0]
        qmp.click(w0["x"] + w0["w"] - 20, w0["y"] + TITLE_CY_OFF)
        time.sleep(1.2)
        hdr, wins = state()
        hid = wins[0]["hid"] if wins else None
        rep("M1: titlebar-min hides the window",
            hid == 1, "| kernel hid=%r focus=%r" %
            (hid, hdr["focus"] if hdr else None))
        m.minimize(0)
        cleanup()

        # ---- M2: 三连击标题栏只能 toggle 一次（max=1，不是 0）----
        launch("files")
        qmp.dblclick(300, 57)
        time.sleep(1.0)
        m.toggle_max(0)
        for _ in range(3):
            qmp.hmc("mouse_button 1")
            time.sleep(0.06)
            qmp.hmc("mouse_button 0")
            time.sleep(0.06)
        time.sleep(1.2)
        hdr, wins = state()
        mx = wins[0]["max"] if wins else None
        rep("M2: triple-click toggles maximize only once",
            mx == 1, "| kernel max=%r (期望 1=仍最大化)" % (mx,))
        cleanup()

        # ---- M3: cascade y 步进必须是 46（cascade=1 -> y=97）----
        launch("clock")          # slot0 cascade=0 -> (60,51)
        launch("notepad")        # slot1 cascade=1 -> (116,97)
        hdr, wins = state()
        y1 = wins[1]["y"] if len(wins) > 1 else None
        x1 = wins[1]["x"] if len(wins) > 1 else None
        rep("M3: cascade step is x+56 / y+46",
            (x1, y1) == (116, 97),
            "| kernel notepad pos=(%r,%r) 期望 (116,97)" % (x1, y1))
        cleanup()

        # ---- M4: 主题 accent 必须真的驱动 16bpp 标题栏配色 ----
        # 变异：gw_update_accent_shades() 把暗/亮档写死成默认蓝 ->
        #       切到 Contrast(accent=0x000000) 后标题栏仍是深蓝 -> 本条变红。
        # 注意：主题是**持久化**的，探完必须切回 Win10 Blue，否则下次
        # 开机就是 Contrast 主题，后面的断言全被带偏。
        launch("settings")          # slot0 (60,51,680x640)
        qmp.click(231, 175)         # 内容区 row4 = Win10 Contrast
        time.sleep(1.5)
        f.shot(2)
        _, _, buf = ppm_load(S[2])
        pt = px(buf, 1280, 400, 57)  # 标题栏中心
        rep("M4: theme accent drives the 16bpp title bar color",
            max(pt) < 60, "| title=%r (Contrast 期望全黑)" % (pt,))
        qmp.click(231, 103)         # 切回 Win10 Blue
        time.sleep(1.5)
        f.shot(3)
        _, _, buf = ppm_load(S[3])
        pt2 = px(buf, 1280, 400, 57)
        rep("M4b: switching back to Win10 Blue restores the blue title",
            pt2[0] < 5 and 95 <= pt2[1] <= 140 and 185 <= pt2[2] <= 235,
            "| title=%r (期望蓝)" % (pt2,))
        cleanup()

        # ---- M5: 快速拖动（单包 24px）必须跟手不掉窗 ----
        # 变异：把 pointer grab 换回老的 ±8/±16 容差盒 -> 单包位移大于
        #       容差时第一个包就掉出 -> 窗口一步不动 -> 本条变红。
        # ★ 判别条件：**按下点取标题栏底沿（move_dy=2）+ 向上拖**。
        #   容差盒对向上拖的第一包判据是 my >= w->y-8，即要求
        #   step <= move_dy+8 = 10；单包 24 直接出界 -> 拖动态当场死。
        #   （横向/向下拖都不行：x 容差 ±16 > 12 默认步进，向下拖第一包
        #    判据 my < w->y+20 也过得去——dbg_dragv.py 实测过四个方向。）
        # ★ 必须**单次拖动**：连续两次拖同一窗口标题栏会踩 400ms 双击判定。
        #   GUI 任务在 TCG 下 CPU 饥饿，鼠标事件处理延迟可达数百 ms，
        #   两次本无关联的 press 会在 tick 时间上落进双击窗口 -> 假最大化
        #   （2026-10-25 dbg_dclick.py 实测 dt=201/399，间隔 3s 都中招）。
        launch("files")                      # slot0 (60,51) 880x640
        _hdr, wins = state()
        g = wins[0] if wins else None
        want = None
        if g:
            xa, ya = g["x"] + 150, g["y"] + 2      # move_dy=2（容差最紧）
            want = (g["x"], max(0, g["y"] - 40))
            qmp.drag(xa, ya, xa, ya - 40, step=24)
            time.sleep(1.0)
        _hdr2, wins2 = state()
        got = (wins2[0]["x"], wins2[0]["y"]) if wins2 else None
        if wins2:
            print("   [M5] after fast up-drag: %r" % (wins2[0],), flush=True)
        rep("M5: fast drag (24px per packet) keeps following the cursor",
            want is not None and got == want,
            "| want=%r got=%r" % (want, got))
        cleanup()

        # ---- M7: 拖动全程无半成品帧 + 跟手 ----
        # 变异：gw_drag_move 的显存内 32 位块搬移换回"旧位置∪新位置"合并区
        #       重绘 -> 每帧全量重画整窗 ~1.1M 像素（含数千次文字/图标），
        #       TCG 下单帧 200-400ms 超过鼠标包间隔 -> screendump 拍到
        #       "桌面已擦、窗口还没画完"的半成品帧（用户报的"闪动"）。
        # 判据：内容匹配率（与拖动前参考帧同区域逐点比，不用颜色常量）。
        #       修复版实测缺失 <=0.05；变异版实测 frame5 缺失 0.81。
        S.extend([os.path.join(ROOT, "temp", "dbgmut_s%d.ppm" % i)
                  .replace("\\", "/") for i in range(4, 22)])
        launch("files")
        _h6, wins6 = state()
        g6 = wins6[0] if wins6 else None
        worst_loss = 0.0          # 最大缺失率；初值 0（不是 1：更新条件是取 max）
        if g6:
            IN_X, IN_Y = g6["x"] + 2, g6["y"] + 16
            IN_W, IN_H = g6["w"] - 4, g6["h"] - 20
            rp = S[shots[0] % len(S)]
            shots[0] += 1
            qmp.shot(rp)
            _rw, _rh, rbuf = ppm_load(rp)
            print("   [M6] g6=%r R0 title=%r content=%r"
                  % ((g6["x"], g6["y"], g6["w"], g6["h"]),
                     px(rbuf, _rw, g6["x"] + 200, g6["y"] + 6),
                     px(rbuf, _rw, g6["x"] + 400, g6["y"] + 300)), flush=True)
            qmp.move_to(g6["x"] + 100, g6["y"] + 2)
            time.sleep(0.25)
            qmp.hmc("mouse_button 1")
            time.sleep(0.15)
            lost_frames = 0
            for i in range(12):
                qmp.hmc("mouse_move 0 6")     # QMP 是相对增量
                qmp.cy += 6
                time.sleep(0.28)
                p = S[shots[0] % len(S)]
                shots[0] += 1
                qmp.shot(p)
                w, h, buf = ppm_load(p)
                wy = g6["y"] + (i + 1) * 6
                # 窗口必须**在新位置**（跟手）：新位标题栏中心是聚焦蓝。
                # 变异版（合并区重绘太慢，整帧拍到"移动前的旧画面"）会让
                # 匹配率反而完美（与 R0 相同）——只有这条能抓住它。
                if px(buf, w, g6["x"] + 200, wy + 6) != (0, 105, 189):
                    lost_frames += 1
                same = tot = 0
                for yy in range(0, IN_H, 3):
                    ro = ((IN_Y + yy) * _rw + IN_X) * 3
                    bo = ((wy + 16 + yy) * w + IN_X) * 3
                    for xx in range(0, IN_W, 7):
                        tot += 1
                        if rbuf[ro + xx * 3:ro + xx * 3 + 3] == \
                                buf[bo + xx * 3:bo + xx * 3 + 3]:
                            same += 1
                if tot:
                    loss = 1.0 - same / tot
                    if loss > worst_loss:
                        worst_loss = loss
            qmp.hmc("mouse_button 0")
            time.sleep(0.4)
        rep("M7: drag leaves no half-drawn frame",
            worst_loss <= 0.10 and lost_frames == 0,
            "| worst-loss=%.2f (<=0.10) stale-frames=%d (==0)"
            % (worst_loss, lost_frames))
        cleanup()

        allok = all(r[1] for r in RESULTS)
        print("total %d assertions, %s" % (len(RESULTS),
                                            "ALL PASS" if allok else "HAS FAILURES"))
        return 0 if allok else 1
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
