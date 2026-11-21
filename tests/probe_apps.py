#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""desktop 应用巡检（probe_apps）——串行单发，勿与其他 GUI 探针并发。

对桌面全家桶逐个巡检（9 个窗口应用 + 7 个桌面游戏图标入口）：
  1. 菜单/图标能建窗：F12 内核转储对账（app id、几何、焦点）
  2. 内容真实渲染：窗口内容区像素色数统计（纯底/空白窗当场红）
  3. 轻交互确实生效：记事本打字、终端敲 help、画图落笔——帧差 > 阈值
  4. 标题关闭按钮能关、关后 F12 转储无残留
游戏在 terminal 宿主窗口里阻塞运行（gw_run_game 阻塞期间 F12 无响应，
不能取转储），只做"进入后画面有绘制 + q 退出后宿主窗消失"两项。

截图纪律：screendump 覆盖写计入沙箱阈值（~50），全程复用两个槽位，
FAIL 用文件复制留证（temp/apps_fail_*.ppm，不占 screendump 额度）。
"""
import os
import random
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

# 多开 VM：每个并发探针进程一个 EZOS_SLOT 槽位（ezos_qemu 既有机制）——
# os-image.bin/disk.vhd 复制到 temp/slot<N>/ 私有副本、QMP 端口段隔离、
# 串口日志改名。slot 0 共用仓库根镜像，Windows 上第二个 QEMU 会因文件锁
# 起不来。要显式串行（用仓库原镜像）就 EZOS_SLOT=0 python probe_apps.py。
os.environ.setdefault("EZOS_SLOT", "1")

from probe_combo import (Qmp, ppm_load, px, frame_diff, alive,  # noqa: E402
                         START_BTN, wait_state, wait_menu, wait_gone,
                         sync_pointer)
from probe_fuzz import (Fuzzer, FuzzModel, icon_rects,  # noqa: E402
                        GFX_W, GFX_H, MENU_ROW_X, MENU_LIST_Y, MENU_ROW_H)
from gui_probe import park_mouse  # noqa: E402
from ezos_qemu import alloc_port, image_path, disk_path, log_path  # noqa: E402
from ezos_env import qemu_exe, accel_args  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = log_path(os.path.join(ROOT, "temp", "apps_serial.log").replace("\\", "/"))
QMP_PORT = alloc_port()
BOOT_WAIT = 20.0
GUI_WAIT = 35.0          # GUI 首帧 + 壁纸/图标铺完（probe_fuzz 实测要 35s）
SHOT_A = (ROOT + "/temp/apps_a.ppm").replace("\\", "/")
SHOT_B = (ROOT + "/temp/apps_b.ppm").replace("\\", "/")

TITLE_H = 12          # 窗口标题栏高（内容区从 y+12 以下起算）
MIN_COLORS = 4        # 内容区采样色数下限（纯底空白窗=1~2）
MIN_DIFF_PX = 80      # 交互前后帧差像素下限（step=2 采样）
GAME_DIFF_PX = 400    # 进游戏前后帧差下限（宿主窗+游戏画面）
SHOT_N = [0]          # screendump 计数（额度 ~50）
fail_n = [0]

# (菜单行 idx, 名称, 交互, 期望最小色数)
# 色数下限按窗口实际外观定，不能一刀切 4：
#   Terminal 是纯黑底 + 白字 + 光标 → 内容区本来就只有 3 种颜色；
#   About Cerulia 同理（深底 + 少量文字）。
# 统一用 4 会把这两个正常窗口判成"没渲染"。
APPS = [
    (0, "File Manager", None, 4),
    (1, "Notepad", "notepad", 4),
    (2, "System Info", None, 4),
    (3, "Clock", None, 4),
    (4, "Settings", None, 4),      # 只查渲染：点主题/速度行会改全局状态，不碰
    (5, "About Cerulia", None, 3),
    (6, "Terminal", "terminal", 3),
    (7, "Paint", "paint", 4),
    (8, "Browser", None, 4),
]

# 桌面图标（icon_rects 顺序）：5..11 是 7 个游戏，对应 gw_launch_game(1..7)
GAMES = [
    (1, "Guess", 5), (2, "TicTac", 6), (3, "Snake", 7), (4, "2048", 8),
    (5, "Mines", 9), (6, "RPS", 10), (7, "Memory", 11),
]


def shot(qmp, path):
    SHOT_N[0] += 1
    return qmp.shot(path)


def save_fail(name):
    """FAIL 留证：复制当前两帧槽位（不占 screendump 额度）"""
    fail_n[0] += 1
    tag = "temp/apps_fail_%d_%s" % (fail_n[0], name.replace(" ", "_"))
    for src in (SHOT_A, SHOT_B):
        try:
            if os.path.exists(src):
                shutil.copyfile(src, ROOT + "/" + tag + os.path.basename(src))
        except OSError:
            pass


def content_colors(buf, w, x0, y0, x1, y1):
    """窗口内容区采样色数（空白/纯底窗会 < MIN_COLORS）"""
    seen = set()
    for y in range(max(y0, 0), min(y1, GFX_H), 2):
        for x in range(max(x0, 0), min(x1, GFX_W), 2):
            seen.add(px(buf, w, x, y))
    return len(seen)


def win_app(wins, app):
    return [w for w in wins if w["app"] == app and w["hid"] == 0]


def title_close_pt(w):
    return (w["x"] + w["w"] - 12 + 5, w["y"] + 6)


def menu_open(qmp, f, rep):
    """点开始按钮打开开始菜单。

    转储里 menu 是 int（不是字符串），且 F12 转储在帧首发、早于本帧鼠标
    补派发 —— 必须轮询跨帧才看得到新状态。
    """
    h, _ = f.gw_state()
    if h and h.get("menu") == 1:
        return True
    for _ in range(2):
        qmp.click(*START_BTN)
        h, _ = wait_menu(f, 1)
        if h and h.get("menu") == 1:
            return True
    rep("menu open", False, "start button click no effect")
    return False


def menu_close(qmp, f):
    h, _ = f.gw_state()
    if h and h.get("menu") == 1:
        qmp.click(*START_BTN)
        wait_menu(f, 0)


def launch_app(qmp, f, rep, idx, name):
    if not menu_open(qmp, f, rep):
        return False
    qmp.click(MENU_ROW_X, MENU_LIST_Y + MENU_ROW_H * idx + MENU_ROW_H // 2)
    h, wins = wait_state(
        f, lambda hh, ww: bool([x for x in ww
                                if x["app"] == idx and x["hid"] == 0]))
    got = win_app(wins, idx)
    if not got:
        rep("launch %s: kernel window created" % name, False,
            "menu row %d no window | %r" % (idx, wins))
        save_fail(name)
        menu_close(qmp, f)
        return False
    w = got[0]
    rep("launch %s: kernel window created" % name, True,
        "bbox=(%d,%d,%d,%d) foc=%d" % (w["x"], w["y"], w["w"], w["h"], w["foc"]))
    return True


def check_content(qmp, f, rep, name, w, min_colors=MIN_COLORS):
    time.sleep(0.6)          # 等窗口首帧渲染完（帧 200-400ms）
    shot(qmp, SHOT_A)
    wA, hA, buf = ppm_load(SHOT_A)
    n = content_colors(buf, wA, w["x"] + 6, w["y"] + TITLE_H + 8,
                       w["x"] + w["w"] - 6, w["y"] + w["h"] - 6)
    ok = n >= min_colors
    rep("%s: content rendered (colors>= %d)" % (name, min_colors), ok,
        "colors=%d" % n)
    if not ok:
        save_fail(name)
    return ok


def check_interact(qmp, f, rep, name, w, kind):
    cx = w["x"] + w["w"] // 2
    cy = w["y"] + TITLE_H + (w["h"] - TITLE_H) // 2
    shot(qmp, SHOT_A)
    wA, hA, a = ppm_load(SHOT_A)
    # 帧差下限按交互类型定：Paint 落笔是成片像素变化（几百~几千），
    # Notepad 敲几个字 / Terminal 打一行 help 只改几十个字符像素
    # （step=2 采样后 50-80 属正常），一刀切 80 会误判 Notepad/Terminal。
    min_diff = {"paint": MIN_DIFF_PX, "notepad": 30, "terminal": 30}[kind]
    if kind == "notepad":
        qmp.click(cx, cy)
        qmp.keys("hi")
        time.sleep(0.8)             # 等文本渲染进窗口（帧 200-400ms）
    elif kind == "terminal":
        qmp.click(cx, cy)
        qmp.line("help")            # 内核 shell help 输出进终端窗口缓冲
        time.sleep(1.2)             # help 输出多行，等渲染完
    elif kind == "paint":
        qmp.drag(cx - 80, cy, cx + 80, cy + 40, step=16)   # 画布落笔
        time.sleep(0.8)
    shot(qmp, SHOT_B)
    wB, hB, b = ppm_load(SHOT_B)
    d, n = frame_diff(a, b, wA, hA, step=2)
    ok = d >= min_diff
    rep("%s: %s interaction changes frame" % (name, kind), ok,
        "diff=%d (>=%d)" % (d, min_diff))
    if not ok:
        save_fail(name)
    return ok


def close_app(qmp, f, rep, idx, name):
    h, wins = f.gw_state()
    got = win_app(wins, idx)
    if not got:
        return
    qmp.click(*title_close_pt(got[0]))
    h, wins = wait_gone(f, idx)
    ok = not win_app(wins, idx)
    rep("close %s: no window left" % name, ok, "wins=%r" % wins)
    if not ok:
        save_fail(name)


def run_app(qmp, f, rep, idx, name, interact, min_colors=MIN_COLORS):
    if not launch_app(qmp, f, rep, idx, name):
        return
    h, wins = f.gw_state()
    w = win_app(wins, idx)[0]
    check_content(qmp, f, rep, name, w, min_colors)
    if interact:
        check_interact(qmp, f, rep, name, w, interact)
    close_app(qmp, f, rep, idx, name)


def run_game(qmp, f, rep, gidx, name, icon):
    rects = icon_rects()
    x, y, bw, bh = rects[icon]
    shot(qmp, SHOT_A)
    wA, hA, a = ppm_load(SHOT_A)
    qmp.dblclick(x + bw // 2, y + bh // 2)
    time.sleep(2.5)                 # games_play 阻塞：F12 无响应，勿取转储
    shot(qmp, SHOT_B)
    wB, hB, b = ppm_load(SHOT_B)
    d, n = frame_diff(a, b, wA, hA, step=2)
    ok = d >= GAME_DIFF_PX
    rep("game %s: drawn after enter" % name, ok, "diff=%d" % d)
    if not ok:
        save_fail(name)
    qmp.key("q")                    # q 退出（行输入型游戏再补回车）
    qmp.key("ret")
    time.sleep(2.5)
    h, wins = f.gw_state()
    ok2 = not any(w2["hid"] == 0 for w2 in wins)   # 宿主 terminal 窗已关
    rep("game %s: host window closed after q" % name, ok2, "wins=%r" % wins)
    if not ok2:
        save_fail(name)


def main():
    random.seed(3)
    proc = subprocess.Popen(
        [QEMU] + accel_args() +
        ["-machine", "pc", "-vga", "std", "-m", "128",
         "-drive", "format=raw,file=" + IMG,
         "-drive", "format=raw,file=" + DISK,
         "-display", "none", "-no-reboot",
         "-serial", "file:" + LOG,
         "-qmp", "tcp:127.0.0.1:%d,server,nowait" % QMP_PORT],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.path.exists(LOG):
        os.remove(LOG)
    qmp = None
    try:
        qmp = Qmp(QMP_PORT)
        if proc.poll() is not None:
            print("FAIL boot: QEMU quit early", flush=True)
            return 1
        time.sleep(BOOT_WAIT)

        # 必须先进 GUI 模式：开机停在 kernel shell，直接发鼠标点击是
        # 打给 shell 的（串口里全是 "Unknown command: q"），
        # 菜单/窗口断言会全红但内核根本没在跑窗口系统。
        qmp.line("desktop")
        time.sleep(GUI_WAIT)

        f = Fuzzer(qmp, FuzzModel(), {}, SHOT_N, log=LOG)
        rep = f.rep
        park_mouse(qmp)
        # 指针账本必须对齐内核真值：GUI 启动会 warp 指针，不对齐的话
        # 之后所有相对位移都错，点开始按钮/图标全是空点（probe_fuzz
        # 2026-10-24 实测过这个坑：几十条断言一起红，其实是探针撒谎）。
        sync_pointer(qmp, f)

        for idx, name, interact, min_colors in APPS:
            run_app(qmp, f, rep, idx, name, interact, min_colors)
            menu_close(qmp, f)

        park_mouse(qmp)
        sync_pointer(qmp, f)
        for gidx, name, icon in GAMES:
            run_game(qmp, f, rep, gidx, name, icon)

        h, wins = f.gw_state()
        rep("final: desktop clean", not any(w2["hid"] == 0 for w2 in wins)
            and h.get("menu") == 0, "wins=%r menu=%s" % (wins, h.get("menu")))
        ok = alive(qmp, [SHOT_A, SHOT_B], GFX_W, GFX_H, 0)  # 帧差探活：主循环未冻结
        rep("alive", ok)

        bad = [r for r in f.results if not r[1]]
        print("\n===== probe_apps: %d ok, %d FAIL =====" %
              (len(f.results) - len(bad), len(bad)), flush=True)
        for name, _ok, detail in bad:
            print("FAIL %s | %s" % (name, detail), flush=True)
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
