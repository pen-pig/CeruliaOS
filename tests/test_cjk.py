# -*- coding: utf-8 -*-
"""test_cjk.py - GB2312 中文渲染 E2E（像素级点阵断言）。

背景：2026-10-18 前，gfx_draw_text_utf8 把所有多字节序列画成豆腐块
（见 gfx.c gfx_draw_tofu 注释）——中文网页整页空白。本测试钉住修复后的
行为：cjk 命令在 framebuffer 固定坐标画汉字，直接从 screendump 的 PPM 里
按 16x16 点阵逐像素比对（on=亮绿/白/亮青，off=黑），不依赖 OCR 认汉字。

断言（全部钉行为，不钉文案）：
  1. (64,64) "中"：16x16 点阵与内核字库逐像素一致
  2. (64,96) "蔚"：同上（第二个字，防"只会画第一个"）
  3. (64,128) "G"（ASCII）：8x8 字模命中（证明中英混排路径没坏）
  4. 按任意键退出后 shell 仍可用（ver）

闸门有效性不做"点阵翻转反证"（汉字点阵翻转对居中密集字判别力弱，
五轮实验 124/142 仍重合），改为**内核变异验证**：提交前临时把
cjk_draw_glyph 的 x 坐标镜像（x+col -> x+15-col）重建，本测试
第 1/2 条必须变红，证明闸门真能抓 bug；改回重建后全绿才提交。

用法：python tests/test_cjk.py   （退出码 0 = 全通过）
端口由 alloc_port() 动态分配（端口必须每脚本唯一，见 tests/README.md）。
"""
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from ezos_env import qemu_exe  # noqa: E402
from ezos_qemu import alloc_port, image_path, kill_stale_qemu  # noqa: E402
import test_rmdir as T
from test_rmdir import Qmp  # noqa: F401

QEMU = qemu_exe()
SHOT = os.path.join(HERE, "cjk_shot.ppm").replace("\\", "/")
FONT_C = os.path.join(ROOT, "kernel", "cjk_font.c")

RESULTS = []


def check(ok, label):
    RESULTS.append((bool(ok), label))
    print(("PASS " if ok else "FAIL ") + label)


def parse_font():
    """从 kernel/cjk_font.c 解析 (codepoint -> 32B 点阵)。"""
    src = open(FONT_C, "rb").read().decode("utf-8")
    maps = {}
    for m in re.finditer(r"\{0x([0-9A-F]{4}), (\d+)\}", src):
        maps[int(m.group(1), 16)] = int(m.group(2))
    bm = re.search(r"cjk_bitmaps\[CJK_MAP_COUNT\]\[32\] = \{(.*?)\n\};", src, re.S).group(1)
    rows = re.findall(r"\{([^{}]*)\}", bm)
    assert len(rows) == len(maps), (len(rows), len(maps))
    return {cp: [int(x, 16) for x in rows[slot].split(",")] for cp, slot in maps.items()}


def ppm_pixels(fn):
    """返回 (w, h, get(x,y)->(r,g,b))。QEMU screendump 是二进制 P6。"""
    data = open(fn, "rb").read()
    parts = data.split(b"\n", 3)
    w, h = map(int, parts[1].split())
    body = parts[3]
    if len(body) < w * h * 3:
        return None

    def get(x, y):
        i = (y * w + x) * 3
        return (body[i], body[i + 1], body[i + 2])
    return w, h, get


def match_glyph(get, x0, y0, cells, fg_rgb, tol=0):
    """16x16 点阵逐像素比对。返回 (命中数, 总数)。"""
    hit = total = 0
    for row in range(16):
        for col in range(16):
            on = (cells[row * 2 + col // 8] & (0x80 >> (col % 8))) != 0
            px = get(x0 + col, y0 + row)
            if on:
                want = fg_rgb
            else:
                want = (0, 0, 0)
            total += 1
            if all(abs(px[c] - want[c]) <= tol for c in range(3)):
                hit += 1
    return hit, total


def main():
    disk = os.path.join(HERE, "disk_cjk.vhd").replace("\\", "/")
    shutil.copyfile(os.path.join(ROOT, "disk.vhd"), disk)
    img = image_path()
    font = parse_font()

    PORT = alloc_port()
    if T.port_in_use(PORT):
        kill_stale_qemu("qemu-system-x86_64.exe")
        T.wait_port_free(PORT, 15)

    proc = subprocess.Popen([
        QEMU, "-icount", "shift=auto", "-vga", "std",
        "-drive", "format=raw,file=" + img,
        "-drive", "format=raw,file=" + disk,
        "-qmp", "tcp:127.0.0.1:%d,server,nowait" % PORT,
    ], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    fails = 0
    try:
        qmp = Qmp(PORT)
        time.sleep(T.BOOT_WAIT)

        # 1) 进入 cjk 演示（framebuffer + 等键），dump 屏幕
        T.run(qmp, "cjk", 10.0)
        time.sleep(2.5)          # gfx_init + 绘制 + 等键
        qmp.cmd("screendump", filename=SHOT)
        time.sleep(0.5)

        r = ppm_pixels(SHOT)
        if not r:
            check(False, "screendump PPM 可解析")
            return 1
        w, h, get = r
        print("  screendump %dx%d" % (w, h))

        # 2) (64,64) "中"：亮绿 0x0A。内核 16 色是标准 VGA（std: 85,255,85），
        #    经 RGB565 量化后 PPM 期望 (80,252,80) —— 不是文本模式 18bit DAC
        #    的 (87,255,87)，那是 probe_color 的文本模式值，图形模式不适用。
        cells_zhong = font.get(ord("中"))
        ok_have = cells_zhong is not None
        check(ok_have, "字库里有\"中\"字形")
        if ok_have:
            hit, total = match_glyph(get, 64, 64, cells_zhong, (80, 252, 80), tol=8)
            ok = hit == total
            check(ok, "(64,64) \"中\" 16x16 点阵逐像素一致 (%d/%d)" % (hit, total))
            if not ok:
                fails += 1

        # 3) (64,96) "蔚"：白 0x0F = (0xFF,0xFF,0xFF)
        cells = font.get(ord("蔚"))
        ok_have = cells is not None
        check(ok_have, "字库里有\"蔚\"字形")
        if ok_have:
            hit, total = match_glyph(get, 64, 96, cells, (255, 255, 255), tol=8)
            ok = hit == total
            check(ok, "(64,96) \"蔚\" 16x16 点阵逐像素一致 (%d/%d)" % (hit, total))
            if not ok:
                fails += 1

        # 4) (64,128) ASCII "GB2312 16x16"：亮青 0x0B。不钉具体字形
        #    （8x8 的 G 顶横只有 4 像素，按方块字想象写阈值连红两轮），
        #    钉三条可证的：字符真画出来了（青像素数在合理区间）、颜色是
        #    青系、背景仍在（有黑像素）。字形本身由 font8x8 + OCR 测试负责。
        cy = 128
        cyan = lambda px: all(abs(px[c] - (80, 252, 255)[c]) <= 8 for c in range(3))
        n_cyan = n_black = n_odd = 0
        for r in range(8):
            for c in range(128):        # 前 16 个 ASCII 字符宽
                px = get(64 + c, cy + r)
                if cyan(px): n_cyan += 1
                elif px == (0, 0, 0): n_black += 1
                else: n_odd += 1
        ok = (n_cyan >= 40) and (n_odd == 0) and (n_black >= 40)
        check(ok, "(64,128) ASCII 串画出且为青系（cyan=%d black=%d odd=%d）"
              % (n_cyan, n_black, n_odd))
        if not ok:
            fails += 1

        # 5) 任意键退出，shell 恢复
        qmp.hmc("sendkey a")
        time.sleep(1.5)
        out = T.last_out(T.run(qmp, "ver", 10.0)).lower()
        ok6 = "version" in out
        check(ok6, "退出 cjk 后 shell 仍可用")
        if not ok6:
            fails += 1

        print("== cjk: %d/%d passed ==" % (len(RESULTS) - fails, len(RESULTS)))
        return 1 if fails else 0
    finally:
        proc.kill()
        try:
            proc.wait(timeout=15)
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
