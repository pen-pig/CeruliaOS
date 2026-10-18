# -*- coding: utf-8 -*-
"""tools/gen_cjk_font.py - 从 Windows 字体提取 16x16 点阵字库 + GB2312 映射表。

产物（都是 C 数组，进内核 .rodata）：
  kernel/cjk_font.h   映射表（unicode u16 -> GB2312 u16，按 unicode 排序）
  kernel/cjk_font.c   点阵数据 7445 字 x 32B（HZK16 区位序）

字库来源优先级：
  1. 字体嵌入位图（EBDT，Windows hint 过的 16px 点阵，质量最好）
  2. glyf 轮廓扫描线光栅化到 16x16（无嵌入位图时的兜底）
"""
import os
import struct
import sys

FONTS = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simfang.ttf",
    r"C:\Windows\Fonts\SimSun.ttf",
]
OUT_H = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kernel", "cjk_font.h")
OUT_C = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kernel", "cjk_font.c")
TARGET_PPEM = 16


def gb2312_chars():
    """全部 GB2312 可编码字符 -> [(codepoint, qw, wm)]，qw/wm 为区位码。"""
    out = []
    for qw in range(0xA1, 0xF8):
        for wm in range(0xA1, 0xFF):
            b = bytes([qw, wm])
            try:
                ch = b.decode("gb2312")
            except UnicodeDecodeError:
                continue
            if ch == "\ufffd" or ord(ch) < 0x80:
                continue
            out.append((ord(ch), qw, wm))
    return out


def _open_font(font_path):
    """打开字体：单字体 ttf 直接用，ttc 逐个试 0..N 直到成功。返回 TTFont 或 None。"""
    from fontTools.ttLib import TTFont
    for num in range(4):
        try:
            return TTFont(font_path, fontNumber=num, lazy=True)
        except Exception:
            if num == 0:
                try:
                    return TTFont(font_path, lazy=True)
                except Exception:
                    return None
    return None


def from_glyf(font_path, chars):
    """glyf 轮廓 -> 16x16 点阵（扫描线 even-odd 填充）。"""
    from fontTools.pens.recordingPen import RecordingPen
    f = _open_font(font_path)
    if f is None:
        return None
    gs = f.getGlyphSet()
    cmap = f.getBestCmap()
    if not cmap:
        return None
    upem = f["head"].unitsPerEm
    scale = TARGET_PPEM / upem
    W = H = TARGET_PPEM
    out = {}
    for cp, qw, wm in chars:
        gname = cmap.get(cp)
        if not gname or gname not in gs:
            continue
        pen = RecordingPen()
        gs[gname].draw(pen)
        # 收集轮廓（细分二次/三次曲线为折线）
        polys = []
        cur = []
        cx = cy = 0.0
        for op, pts in pen.value:
            if op == "moveTo":
                if len(cur) > 1:
                    polys.append(cur)
                cur = [pts[0]]
                cx, cy = pts[0]
            elif op == "lineTo":
                cur.append(pts[0])
                cx, cy = pts[0]
            elif op == "qCurveTo":
                # fontTools 约定：pts[:-1] 为控制点（None = 用当前点），
                # pts[-1] 为终点（None = 最后两个控制点的中点，TrueType 连续
                # 二次曲线简写）。相邻控制点之间有隐含 on-curve 中点。
                ctrl = [p if p is not None else (cx, cy) for p in pts[:-1]]
                end = pts[-1]
                if end is None and ctrl:
                    if len(ctrl) >= 2:
                        end = ((ctrl[-2][0] + ctrl[-1][0]) / 2.0,
                               (ctrl[-2][1] + ctrl[-1][1]) / 2.0)
                    else:
                        end = (cx, cy)

                def quad(x0, y0, c, e):
                    for k in range(1, 9):
                        t = k / 9.0
                        mt = 1.0 - t
                        x = mt * mt * x0 + 2 * mt * t * c[0] + t * t * e[0]
                        y = mt * mt * y0 + 2 * mt * t * c[1] + t * t * e[1]
                        cur.append((x, y))

                for i, c in enumerate(ctrl):
                    if i < len(ctrl) - 1:
                        seg_end = ((c[0] + ctrl[i + 1][0]) / 2.0,
                                   (c[1] + ctrl[i + 1][1]) / 2.0)
                    else:
                        seg_end = end
                    quad(cx, cy, c, seg_end)
                    cx, cy = seg_end
            elif op == "curveTo":
                p0, p1, p2, p3 = pts
                for i in range(1, 13):
                    t = i / 12.0
                    mt = 1 - t
                    x = mt**3 * cx + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
                    y = mt**3 * cy + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
                    cur.append((x, y))
                cx, cy = p3
            elif op == "closePath":
                if len(cur) > 1:
                    polys.append(cur)
                cur = []
        if len(cur) > 1:
            polys.append(cur)
        if not polys:
            continue
        # 扫描线：字体坐标 y 向上；采样行 row（0=顶）对应 y_font=(H-row-0.5)/scale
        bits = bytearray(32)
        for row in range(H):
            yf = (H - row - 0.5) / scale
            xs = []
            for poly in polys:
                n = len(poly)
                for i in range(n):
                    x1, y1 = poly[i]
                    x2, y2 = poly[(i + 1) % n]
                    if (y1 <= yf) != (y2 <= yf):
                        xs.append(x1 + (yf - y1) / (y2 - y1) * (x2 - x1))
            xs.sort()
            # 全局 even-odd：排序后两两配对填充（嵌套轮廓不会被填实）
            for k in range(0, len(xs) - 1, 2):
                a = max(0, int(xs[k] * scale))
                b = min(W - 1, int(xs[k + 1] * scale + 0.999))
                for xx in range(a, b + 1):
                    bits[row * 2 + xx // 8] |= 0x80 >> (xx % 8)
        out[cp] = bytes(bits)
    print("glyf: %d/%d glyphs rasterized" % (len(out), len(chars)))
    return out or None


def emit_header(path, mapping):
    """mapping: [(codepoint, qw, wm)] 已按 codepoint 排序。"""
    lines = []
    lines.append("/* cjk_font.h - GB2312 映射表（由 tools/gen_cjk_font.py 生成，勿手改）")
    lines.append(" * unicode(codepoint) -> 点阵 slot，按 codepoint 升序，供二分查找。 */")
    lines.append("#ifndef CJK_FONT_H")
    lines.append("#define CJK_FONT_H")
    lines.append("")
    lines.append("#include \"types.h\"")
    lines.append("")
    lines.append("#define CJK_MAP_COUNT %du" % len(mapping))
    lines.append("")
    lines.append("typedef struct {")
    lines.append("    uint16_t uni;   /* unicode 码点 */")
    lines.append("    uint16_t slot;  /* cjk_bitmaps 下标（有效码位压缩序号，生成时算定） */")
    lines.append("} cjk_map_ent_t;")
    lines.append("")
    lines.append("extern const cjk_map_ent_t cjk_map[CJK_MAP_COUNT];")
    lines.append("extern const uint8_t cjk_bitmaps[CJK_MAP_COUNT][32]; /* 压缩序号点阵 */")
    lines.append("")
    lines.append("#endif")
    open(path, "w", newline="\n").write("\n".join(lines) + "\n")


def emit_source(path, mapping, bitmaps):
    lines = ["/* cjk_font.c - 16x16 GB2312 点阵（由 tools/gen_cjk_font.py 生成，勿手改） */",
             "#include \"cjk_font.h\"", ""]
    # 映射表按 codepoint 排序
    mapping = sorted(mapping)
    lines.append("const cjk_map_ent_t cjk_map[CJK_MAP_COUNT] = {")
    for slot, (cp, qw, wm) in enumerate(mapping):
        lines.append("    {0x%04X, %d}," % (cp, slot))
    lines.append("};")
    lines.append("")
    # 点阵：slot = 有效码位压缩序号（区位升序枚举），没有字形填 0。
    # 历史坑：第一版用未压缩区位公式 (qw-0xA1)*94+(wm-0xA1) 当下标，
    # 而 GB2312 的 01-87 区里有 733 个空位——高区汉字 slot 越界被静默
    # 丢弃、低区 slot 全零浪费。改成"生成时算定 slot"后三方（生成器、
    # 内核查找、E2E 解析）零计算，杜绝这类错位。
    slots = [b"\x00" * 32 for _ in range(len(mapping))]
    have = 0
    for slot, (cp, qw, wm) in enumerate(mapping):
        bm = bitmaps.get(cp)
        if bm and len(bm) == 32:
            slots[slot] = bm
            have += 1
    lines.append("const uint8_t cjk_bitmaps[CJK_MAP_COUNT][32] = {")
    for i, s in enumerate(slots):
        body = ",".join("0x%02X" % b for b in s)
        lines.append("    {%s}," % body)
        if i % 94 == 93:
            pass
    lines.append("};")
    open(path, "w", newline="\n").write("\n".join(lines) + "\n")
    print("emitted %d bitmap slots (%d glyphs present)" % (len(slots), have))


def main():
    chars = gb2312_chars()
    print("GB2312 encodable chars: %d" % len(chars))
    bitmaps = None
    used = None
    for fp in FONTS:
        if not os.path.exists(fp):
            continue
        print("--- trying %s" % fp)
        bitmaps = from_glyf(fp, chars)
        if bitmaps:
            used = fp
            break
    if not bitmaps:
        print("FAIL: no font produced bitmaps")
        return 1
    print("font used: %s" % used)
    emit_header(OUT_H, chars)
    emit_source(OUT_C, chars, bitmaps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
