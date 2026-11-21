"""probe_interact.py - Notepad/Terminal 键盘交互的**像素级**判据

probe_apps.check_interact 只断言"打字后帧差 >= 30"——帧差证明"有东西重绘了"，
证明不了"画出来的字是对的"。本探针把判据推到像素级：

  A Notepad 打字：格 0='a'、格 1='b'（8x8 字模逐像素比对，字模直接从
     kernel/gfx.c 的 builtin_font 解析，与内核同源）；退格后格 1 变空。
  B Terminal echo：回车后输出行 "zq" 出现在**第 0 列**（输入行的 'z' 在
     第 5 列，第 0 列出现 'z' 只能来自 echo 输出——这正是历史上
     terminal_putchar 不走 gfx hook 导致 GUI 下输出不可见的路径）。
  C 键盘只跟焦点：点谁的标题栏就打进谁，另一窗可见区域像素不动。

字体/坐标事实（源码复核）：
  - builtin_font 字节 bit0 = 最左像素（gfx_draw_char: line & (1<<col)）
  - 32bpp 下 fg=0x00 走标准 VGA 表 = 纯黑 (0,0,0)；bg 0x0E/0x0F 均为亮色
  - Notepad 行 r 字格原点 = (x+3, y+29+r*10)，行高 10，当前行 bg 黄
  - Terminal 字格原点 = (x+1, y+13) + (col*8, row*16)，cols 恒 80
"""
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from probe_combo import Qmp, ppm_load, px, rm, START_BTN  # noqa: E402
from probe_fuzz import (FuzzModel, Fuzzer, APP_IDX, MENU_LIST_Y,  # noqa: E402
                        MENU_ROW_H, MENU_ROW_X, BOOT_WAIT)
from gui_probe import park_mouse, mouse_sync  # noqa: E402
from ezos_qemu import alloc_port, image_path, disk_path, log_path  # noqa: E402
from ezos_env import qemu_exe, accel_args  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = log_path(os.path.join(ROOT, "temp", "interact_serial.log").replace("\\", "/"))
QMP_PORT = alloc_port()

NOTEPAD = APP_IDX["notepad"]
TERMINAL = APP_IDX["terminal"]


# ---------------------------------------------------------------- 字模
def load_kernel_font():
    """从 kernel/gfx.c 的 builtin_font designated initializer 解析字模。

    不手抄字模表：手抄会漂移，与内核不同源时断言误报。
    """
    src = open(os.path.join(ROOT, "kernel", "gfx.c"), "r",
               encoding="utf-8", errors="replace").read()
    m = re.search(r"builtin_font\b[^=]*=\s*\{", src)
    if not m:
        raise RuntimeError("builtin_font not found in kernel/gfx.c")
    depth, start = 1, m.end()
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                body = src[start:i]
                break
    else:
        raise RuntimeError("builtin_font braces unbalanced")
    font = {}
    for e in re.finditer(r"\[0x([0-9A-Fa-f]{2})\]\s*=\s*\{([^}]*)\}", body):
        vals = [int(v, 16) & 0xFF for v in e.group(2).split(",") if v.strip()]
        if len(vals) == 8:
            font[int(e.group(1), 16)] = vals
    return font


FONT = load_kernel_font()


def glyph_match(buf, W, x, y, ch):
    """8x8 字模逐像素比对，返回匹配率。

    笔画位（bit0=最左）必须黑（RGB 三和 <100），空位必须非黑（>200）。
    两种底色（Notepad 当前行黄 0x0E / 其余白 0x0F / 终端灰白底）都满足
    ">200"，所以同一个判据通吃。
    """
    rows = FONT.get(ord(ch))
    if rows is None:
        return 0.0
    good = 0
    for r in range(8):
        line = rows[r]
        for c in range(8):
            dark = sum(px(buf, W, x + c, y + r)) < 100
            if ((line >> c) & 1) == (1 if dark else 0):
                good += 1
    return good / 64.0


def cell_dark(buf, W, x, y):
    return sum(sum(px(buf, W, x + c, y + r)) < 100 for r in range(8)
               for c in range(8))


def region_dark(buf, W, x0, y0, x1, y1):
    n = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            if sum(px(buf, W, x, y)) < 100:
                n += 1
    return n


def shot_buf(qmp, S, idx):
    """拍一帧（先停车位，防 16x16 软件光标污染采样区）。"""
    park_mouse(qmp)
    _, _, buf = ppm_load(qmp.shot(S[idx % len(S)]))
    return buf


# ---------------------------------------------------------------- 主流程
def main():
    results = []

    def rep(name, ok, detail=""):
        results.append((name, ok))
        print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                           (" | " + detail) if detail else ""), flush=True)
        return ok

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    rm(LOG)
    import subprocess
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
    # 33 个槽：Fuzzer 的 assert_* 固定用 30/31/32（与 probe_combo2 同约束）
    S = [os.path.join(ROOT, "temp", "interact_s%d.ppm" % i).replace("\\", "/")
         for i in range(33)]
    shots = [0]
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
        rep("boot reaches shell", ok)
        if not ok:
            return 1
        qmp = Qmp(QMP_PORT)
        qmp.cx, qmp.cy = 640, 512
        qmp.line("desktop")
        time.sleep(35)
        park_mouse(qmp)
        w, h, ref_buf = ppm_load(qmp.shot(S[0]))
        shots[0] += 1
        rep("desktop entered (1280x1024)", (w, h) == (1280, 1024))
        if (w, h) != (1280, 1024):
            return 1
        W = w

        m = FuzzModel()
        f = Fuzzer(qmp, m, S, shots, log=LOG)
        f.rep = rep
        f.ref = ref_buf

        hdr, _ = f.gw_state()
        sync_ok, sync_detail = mouse_sync(hdr, qmp)
        rep("pointer synced with kernel", sync_ok is True, "| " + sync_detail)

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
            """菜单启动（行号 = app id），内核转储确认窗口建出。"""
            if not menu_open():
                rep("launch %s: menu open" % app, False)
                return None
            qmp.click(MENU_ROW_X,
                      MENU_LIST_Y + APP_IDX[app] * MENU_ROW_H + MENU_ROW_H // 2)
            time.sleep(1.8)
            m.menu = False
            m.launch(app)
            hdr, wins = f.gw_state()
            got = [x for x in (wins or []) if x["app"] == APP_IDX[app]]
            rep("launch %s: kernel window created" % app, len(got) == 1,
                "| kernel=%r" % ([(x["slot"], x["x"], x["y"], x["w"], x["h"])
                                  for x in (wins or [])],))
            return got[0] if got else None

        def retry_pixels(name, check, tries=3):
            """像素断言带重拍：截图与内核重绘并发，坏帧/时序差重试即可分清。"""
            best = (False, "")
            for _ in range(tries):
                buf = shot_buf(qmp, S, shots[0])
                shots[0] += 1
                best = check(buf)
                if best[0]:
                    break
                time.sleep(1.2)
            rep(name, best[0], "| " + best[1])
            return best[0]

        def bbox_of(wins, app):
            for x in (wins or []):
                if x["app"] == app:
                    return x
            return None

        # ============ 场景 A：Notepad 打字 + 退格（像素级字模判据） ============
        npw = launch("notepad")
        if npw is None:
            return 1
        tx, ty = npw["x"] + 3, npw["y"] + 29     # 行 0 字格原点
        qmp.click(npw["x"] + npw["w"] // 2, npw["y"] + 6)   # 标题栏聚焦
        qmp.keys("ab")

        def check_ab(buf):
            ma, mb = glyph_match(buf, W, tx, ty, "a"), glyph_match(buf, W, tx + 8, ty, "b")
            empty3 = cell_dark(buf, W, tx + 16, ty)
            return (ma >= 0.9 and mb >= 0.9 and empty3 == 0,
                    "a=%.2f b=%.2f cell3_dark=%d" % (ma, mb, empty3))
        retry_pixels("notepad: typed 'ab' renders at cells 0,1", check_ab)

        qmp.key("backspace")

        def check_bs(buf):
            ma = glyph_match(buf, W, tx, ty, "a")
            empty1 = cell_dark(buf, W, tx + 8, ty)
            return (ma >= 0.9 and empty1 == 0,
                    "a=%.2f cell2_dark=%d" % (ma, empty1))
        retry_pixels("notepad: backspace erases cell 1, keeps 'a'", check_bs)

        f.cleanup_by_kernel("A cleanup")
        f.assert_desk("A desk clean")

        # ============ 场景 B：Terminal echo 输出可见（第 0 列判据） ============
        tw = launch("terminal")
        if tw is None:
            return 1
        cx0, cy0 = tw["x"] + 1, tw["y"] + 13
        rows = min((tw["h"] - 13 - 8) // 16, 24)
        qmp.click(tw["x"] + tw["w"] // 2, tw["y"] + 6)
        base = shot_buf(qmp, S, shots[0]); shots[0] += 1
        base_dark = region_dark(base, W, cx0, cy0, cx0 + 80 * 8, cy0 + rows * 16)
        qmp.keys("echo")
        qmp.key("spc")
        qmp.keys("zq")
        qmp.key("ret")

        def find_zq_row(buf):
            """输出行特征：第 0 列 'z'、第 1 列 'q'（输入行的 z 在第 5 列）。"""
            for r in range(rows):
                y = cy0 + r * 16
                if glyph_match(buf, W, cx0, y, "z") >= 0.9 and \
                   glyph_match(buf, W, cx0 + 8, y, "q") >= 0.9:
                    return r
            return -1

        def check_echo(buf):
            r = find_zq_row(buf)
            if r < 0:
                return (False, "no row with 'zq' at col 0-1")
            dark = region_dark(buf, W, cx0, cy0, cx0 + 80 * 8, cy0 + rows * 16)
            return (dark > base_dark + 20,
                    "row=%d dark %d->%d" % (r, base_dark, dark))
        retry_pixels("terminal: echo output visible at col 0 (putchar path)",
                     check_echo)

        f.cleanup_by_kernel("B cleanup")
        f.assert_desk("B desk clean")

        # ============ 场景 C：键盘只跟焦点（点谁打谁，另一窗不动） ============
        npw = launch("notepad")
        tw = launch("terminal")
        if npw is None or tw is None:
            return 1
        tx, ty = npw["x"] + 3, npw["y"] + 29
        cx0, cy0 = tw["x"] + 1, tw["y"] + 13
        rows = min((tw["h"] - 13 - 8) // 16, 24)
        qmp.click(tw["x"] + tw["w"] // 2, tw["y"] + 6)   # 聚焦 terminal
        np_before = shot_buf(qmp, S, shots[0]); shots[0] += 1
        qmp.keys("echo"); qmp.key("spc"); qmp.keys("zq"); qmp.key("ret")

        def check_focus_term(buf):
            r = find_zq_row(buf)
            if r < 0:
                return (False, "terminal: no echo row")
            # notepad 未被 terminal 盖住的裸露条（cascade 下是左侧竖条）
            nx0, ny0 = npw["x"], npw["y"]
            nx1, ny1 = npw["x"] + npw["w"], npw["y"] + npw["h"]
            lx1 = min(nx1, tw["x"])
            if lx1 <= nx0:
                return (False, "notepad fully covered by terminal")
            d0 = region_dark(np_before, W, nx0, ny0, lx1, ny1)
            d1 = region_dark(buf, W, nx0, ny0, lx1, ny1)
            return (d0 == d1, "notepad strip dark %d -> %d" % (d0, d1))
        retry_pixels("focus: typing goes to terminal, notepad pixels still",
                     check_focus_term)

        qmp.click(npw["x"] + npw["w"] // 2, npw["y"] + 6)   # 切回 notepad
        term_base = shot_buf(qmp, S, shots[0]); shots[0] += 1
        # terminal 未被 notepad 盖住的裸露条（cascade 下是右侧竖条）
        trx0 = max(tw["x"], npw["x"] + npw["w"])
        trx1 = tw["x"] + tw["w"]
        qmp.keys("cd")

        def check_focus_np(buf):
            # 新开的 notepad 是空白缓冲，打字从格 0 起
            mc, md = glyph_match(buf, W, tx, ty, "c"), glyph_match(buf, W, tx + 8, ty, "d")
            d1 = region_dark(buf, W, trx0, tw["y"], trx1, tw["y"] + tw["h"])
            d0 = region_dark(term_base, W, trx0, tw["y"], trx1, tw["y"] + tw["h"])
            return (mc >= 0.9 and md >= 0.9 and d1 == d0,
                    "c=%.2f d=%.2f term strip %d -> %d" % (mc, md, d0, d1))
        retry_pixels("focus: typing goes to notepad, terminal pixels still",
                     check_focus_np)

        f.cleanup_by_kernel("C cleanup")
        f.assert_desk("C desk clean")

        fails = [n for n, okk in results if not okk]
        print("interact: %d/%d assertions passed" % (len(results) - len(fails),
                                                     len(results)), flush=True)
        return 1 if fails else 0
    finally:
        if qmp is not None:
            try:
                qmp.cmd("quit")
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
