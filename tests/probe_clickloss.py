#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""点击丢失回归（probe_clickloss）——钉住 2026-11 的修复。

背景：GUI 主循环一帧才采样一次鼠标按键电平做沿检测。整段"按下+松开"
落在两次采样之间（重绘期间）时按下沿永远丢失，用户现象是
"开始菜单点不开 / 标题栏按钮点了没反应 / 双击标题最大化失效"。
修复方式：驱动层把每次按键变迁连同发生时刻排进事件队列，主循环按时间序
补派发（kernel/mouse.c + kernel/gfxwin.c）。

本测试专门制造"同一帧内按下+松开"（QMP 连发 press/release，中间零间隔）
并逐项断言：
  1. 开始按钮连打：每 10 次快速点击菜单开合必须严格偶数次（丢一个就变奇数）
  2. 标题栏快速双击：必须最大化，再双击必须还原
  3. 标题栏关闭按钮快速点击：窗口必须消失
  4. 桌面图标快速双击：应用必须启动
  5. 收尾：桌面无残留窗口

用法：
    EZOS_SLOT=6 python tests/probe_clickloss.py
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

# 每个并发探针进程一个槽位：镜像/磁盘副本、QMP 端口段、串口日志全隔离
os.environ.setdefault("EZOS_SLOT", "6")

from probe_combo import (Qmp, alive, START_BTN, wait_menu, wait_gone,  # noqa: E402
                         sync_pointer)
from probe_fuzz import (Fuzzer, FuzzModel, icon_rects,  # noqa: E402
                        GFX_W, GFX_H, MENU_ROW_X, MENU_LIST_Y, MENU_ROW_H)
from ezos_qemu import alloc_port, image_path, disk_path, log_path  # noqa: E402
from ezos_env import qemu_exe, accel_args  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = log_path(os.path.join(ROOT, "temp", "clickloss_serial.log").replace("\\", "/"))
QMP_PORT = alloc_port()
BOOT_WAIT = 20.0
SETTLE = 1.0           # 结构性操作后等 GUI 主循环处理完（补派发按帧排队）
SETTLE_SHORT = 0.15    # 逐次点击校验时的等待
CLICKS = 30            # 开始按钮连打次数
MAX_W = 1200          # 最大化后的宽度下限


def fast_click(qmp, x, y):
    """按下+松开零间隔：两包落在同一帧内，电平采样必丢沿（回归靶点）"""
    qmp.move_to(x, y, step=16, delay=0.02)
    qmp.hmc("mouse_button 1")
    qmp.hmc("mouse_button 0")


def fast_dblclick(qmp, x, y):
    """四连发（press/release/press/release），中间零间隔"""
    qmp.move_to(x, y, step=16, delay=0.02)
    qmp.hmc("mouse_button 1")
    qmp.hmc("mouse_button 0")
    qmp.hmc("mouse_button 1")
    qmp.hmc("mouse_button 0")


def win_app(wins, app):
    return [w for w in wins if w["app"] == app and w["hid"] == 0]


def main():
    proc = subprocess.Popen(
        [QEMU] + accel_args() +
        ["-machine", "pc", "-vga", "std", "-m", "128",
         "-drive", "format=raw,file=" + IMG,
         "-drive", "format=raw,file=" + DISK,
         "-display", "none", "-no-reboot",
         "-serial", "file:" + LOG,
         "-qmp", "tcp:127.0.0.1:%d,server,nowait" % QMP_PORT],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    qmp = None
    results = []

    def rep(name, ok, detail=""):
        results.append((name, bool(ok)))
        print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                           (" | " + detail) if detail else ""), flush=True)

    try:
        qmp = Qmp(QMP_PORT)
        if proc.poll() is not None:
            print("FAIL boot: QEMU quit early", flush=True)
            return 1
        time.sleep(BOOT_WAIT)
        qmp.line("desktop")          # 进图形界面（否则鼠标点击无处可去）
        time.sleep(2.0)

        f = Fuzzer(qmp, FuzzModel(), {}, [0], log=LOG)

        # 等 GUI 就绪：F12 转储能拿到 menu 字段
        h = None
        for _ in range(20):
            h, wins = f.gw_state()
            if h:
                break
            time.sleep(1.0)
        if not h:
            rep("gui ready (F12 dump)", False, "no dump in 20s")
            return 1
        rep("gui ready (F12 dump)", True)

        # GUI 启动把指针 warp 到屏幕中心，探针账本起点不同 —— 必须先对齐
        ok_sync = sync_pointer(qmp, f)
        h, _w = f.gw_state()
        rep("pointer ledger synced to kernel", ok_sync,
            "kernel=(%s,%s) ledger=(%d,%d)" %
            (h.get("mx") if h else None, h.get("my") if h else None,
             qmp.cx, qmp.cy))

        # ---- 1. 开始按钮连打：每次点击菜单必须严格交替开/合 ----
        # 只看组末 parity 会被"点击全打偏"骗过去（菜单压根没开也算偶数），
        # 所以逐次点击逐次读状态，严格交替才算过。
        ok_all = True
        expect = 1            # 第一次点击后菜单应该是打开的，之后严格交替
        for i in range(CLICKS):
            h0, _w0 = f.gw_state()
            tr0 = h0.get("tr") if h0 else None
            tg0 = h0.get("tg") if h0 else None
            fast_click(qmp, *START_BTN)
            h, _w = wait_menu(f, expect)
            menu = h.get("menu") if h else None
            if menu != expect:
                ok_all = False
                tr1 = h.get("tr") if h else None
                # tr 没涨 = 内核根本没看到按键变迁（丢在 PS/2 字节流层）；
                # tr 涨了但菜单没变 = 变迁进来了但补派发/命中判定有问题。
                rep("start button fast click #%d toggles menu" % (i + 1),
                    False, "menu=%s expect=%s evd=%s tr=%s->%s tg=%s->%s "
                    "ep=%s dp=%s raw=%s pkt=%s" %
                    (menu, expect, h.get("evd") if h else None, tr0, tr1,
                     tg0, h.get("tg") if h else None,
                     h.get("ep") if h else None, h.get("dp") if h else None,
                     h.get("raw") if h else None, h.get("pkt") if h else None))
                break
            expect = 1 - expect
        if ok_all:
            h, _w = f.gw_state()
            rep("start button fast clicks x%d toggle menu strictly" % CLICKS,
                True, "evd=%s" % (h.get("evd") if h else None))

        # ---- 2. 标题栏快速双击：最大化 / 还原 ----
        if ok_all:
            # 用常规点击开一个记事本（准备阶段，不测快速路径）
            qmp.click(*START_BTN)
            time.sleep(1.0)
            qmp.click(MENU_ROW_X, MENU_LIST_Y + MENU_ROW_H * 1 + MENU_ROW_H // 2)
            time.sleep(2.0)
            h, wins = f.gw_state()
            got = win_app(wins, 1)
            if not got:
                rep("open notepad for dblclick", False, "wins=%r" % wins)
            else:
                w = got[0]
                rep("open notepad for dblclick", True,
                    "bbox=(%d,%d,%d,%d)" % (w["x"], w["y"], w["w"], w["h"]))
                fast_dblclick(qmp, w["x"] + w["w"] // 2, w["y"] + 6)
                time.sleep(SETTLE)
                h, wins = f.gw_state()
                got = win_app(wins, 1)
                w2 = got[0] if got else {"x": -1, "y": -1, "w": 0, "h": 0}
                ok = bool(got) and w2["w"] >= MAX_W
                rep("title bar fast dblclick -> maximize", ok,
                    "bbox=(%d,%d,%d,%d)" % (w2["x"], w2["y"], w2["w"], w2["h"]))
                if ok:
                    fast_dblclick(qmp, w2["x"] + w2["w"] // 2, w2["y"] + 6)
                    time.sleep(SETTLE)
                    h, wins = f.gw_state()
                    got = win_app(wins, 1)
                    w3 = got[0] if got else {"w": MAX_W}
                    ok2 = bool(got) and w3["w"] < MAX_W
                    rep("title bar fast dblclick -> restore", ok2,
                        "w=%d" % w3["w"])
                    # ---- 3. 标题栏关闭按钮快速点击 ----
                    w4 = got[0]
                    fast_click(qmp, w4["x"] + w4["w"] - 12 + 5, w4["y"] + 6)
                    _h3, wins = wait_gone(f, 1)
                    ok3 = not win_app(wins, 1)
                    rep("title close fast click -> window gone", ok3,
                        "wins=%r" % wins)

        # ---- 4. 桌面图标双击：先用常规双击做对照组 ----
        # 对照组用慢双击（60ms 间隔）：能启动 = 坐标/主题没问题，快双击失败
        # 就是快路径的锅；慢双击也失败 = 坐标根本没落在图标上（看转储 mx/my）
        rects = icon_rects()
        ix, iy, iw, ih = rects[3]           # Terminal 图标
        cx, cy = ix + iw // 2, iy + ih // 2
        qmp.dblclick(cx, cy)
        time.sleep(2.5)
        h, wins = f.gw_state()
        got = win_app(wins, 6)
        rep("control: slow icon dblclick -> terminal launched", bool(got),
            "wins=%r" % wins)
        if not got:
            mx = h.get("mx") if h else None
            my = h.get("my") if h else None
            rep("control: pointer landed on target", False,
                "mx=%s my=%s target=(%d,%d)" % (mx, my, cx, cy))
        else:
            w = got[0]
            qmp.click(w["x"] + w["w"] - 12 + 5, w["y"] + 6)   # 关掉
            wait_gone(f, 6)
            fast_dblclick(qmp, cx, cy)
            time.sleep(2.5)
            h, wins = f.gw_state()
            got = win_app(wins, 6)
            rep("desktop icon fast dblclick -> terminal launched", bool(got),
                "wins=%r" % wins)
        if got:
            w = got[0]
            fast_click(qmp, w["x"] + w["w"] - 12 + 5, w["y"] + 6)
            wait_gone(f, 6)

        # ---- 5. 收尾干净 ----
        h, wins = f.gw_state()
        clean = not any(x["hid"] == 0 for x in wins)
        rep("final: no leftover window", clean, "wins=%r" % wins)
        rep("alive", alive(qmp, [ROOT + "/temp/cl_a.ppm", ROOT + "/temp/cl_b.ppm"],
                           GFX_W, GFX_H, 0))

        bad = [r for r in results if not r[1]]
        print("\n===== probe_clickloss: %d ok, %d FAIL =====" %
              (len(results) - len(bad), len(bad)), flush=True)
        return 0 if not bad else 1
    finally:
        try:
            if qmp is not None:
                qmp.cmd("quit")
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
