# -*- coding: utf-8 -*-
"""gui_probe.py - GUI 探针共享基础设施（光标停车 + 内核窗口状态对账）

解决两个 2026-10-24 实测的**探针侧**根本障碍（不是内核 bug，但会让
像素断言彻底不可信）：

1) **软件光标污染像素采样**
   内核 gw_cursor_draw() 把 16x16 箭头**直接画进 LFB**（kernel/gfxwin.c
   的 gw_cursor_map），所以 screendump 拍到的画面里，光标所在的那 16x16
   方块是光标图形而不是真实内容。实测采样点 (496,103) 正好落在光标上
   → 读到 (0,0,0) 黑色 → 被误判成"标题栏不是蓝"。
   修法：每次 screendump **之前**先把鼠标停到固定"停车位"，让光标
   落在所有采样点之外；**开机参考帧也必须在停车位拍**，否则 ref_diff
   会被光标污染（光标从停车位移到别处 = 参考帧与当前帧差一片）。
   停车位选在任务栏最右端的空白区（时钟文字左侧的空白），那里既不
   是任何窗口的标题栏，也不是桌面图标区。

2) **无法区分"模型错"和"内核错"**
   探针侧维护了一份与内核同构的窗口模型（slots/focused/hidden/max）。
   一旦模型与内核漂移，后续所有断言的前提全错，而"看截图猜"是不可
   靠的（2026-10-24 C7 场景：模型说 notepad 已关，屏幕上它还在，
   只是失焦——光看截图分不清是谁错）。
   修法：内核加 F12 调试热键，把 gw_windows[] 的真实状态经
   **serial_write 直写 COM1**（GUI 模式下 terminal_writestring 已被
   g_gfx_hook 重定向进 GUI 终端缓冲，ezos_console_write 永远不进串口）
   转储成 [GW] 开头的机器可解析行。探针按下 F12 后读串口，逐字段与
   模型对账——**机器判定，不靠读码推理**。
"""
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if ROOT not in sys.path:
    sys.path.append(ROOT)

# ---- 光标停车位 ----
# 任务栏 y=992..1024。任务栏按钮区 x<1190（TB_BTN_XMAX），右侧是
# 时钟文字（GFX_W-52=1228 起，5 个字符 x8px → 1228..1268）与
# 通知小图标（GFX_W-24=1256）。选 x=1200 落在按钮区与时钟之间，
# 是任务栏里唯一的空白；y=1008 在任务栏中带。
# 16x16 光标从 (1200,1008) 画到 (1215,1023)，不覆盖任何标题栏
# 采样点（标题栏 y<100）、不覆盖桌面图标区（x<520）、不覆盖时钟
# 文字（x>=1228）。
PARK_X, PARK_Y = 1200, 1008

# ---- 内核窗口状态转储解析 ----
_GW_HDR = re.compile(
    r"\[GW\] (?:seq=(\d+) )?n=(\d+) focus=(\d+) menu=(\d+) mx=(-?\d+) my=(-?\d+)")
_GW_WIN = re.compile(
    r"\[GW\] w slot=(\d+) app=(\d+) x=(-?\d+) y=(-?\d+) w=(\d+) h=(\d+) "
    r"hid=(\d+) foc=(\d+) max=(\d+)(?: tbx=(-?\d+) tw=(\d+))?")


def parse_gw_dump(text):
    """解析内核 [GW] 窗口状态转储。

    返回 (header, windows)：
      header   = {"n":int, "focus":int, "menu":int, "mx":int, "my":int,
                  "seq":int}
      windows  = [{"slot","app","x","y","w","h","hid","foc","max",
                   "tbx","tw"}, ...]
    解析不到返回 (None, [])。

    tbx = 该窗口在**任务栏**上的按钮 x 坐标（-1 = 没按钮），tw = 按钮宽度。
    探针要按这两个真值去点任务栏：模型自己算的按钮位置会漂
    （内核按钮宽度上限 gw_scale_x(180)=360，模型一直写 180）。

    seq = 内核每次转储的自增序号。探针按 F12 后只认**自己那一份**
    （seq == 它发出的 F12 次数）：内核忙时转储会迟到，先读到旧的那份
    就会拿旧状态跟当前模型比，报出根本不存在的"漂移"（2026-10-24
    C8 op#75 实测过）。
    """
    header = None
    wins = []
    for line in text.splitlines():
        line = line.strip()
        m = _GW_HDR.match(line)
        if m:
            header = {"seq": int(m.group(1)) if m.group(1) else 0,
                      "n": int(m.group(2)), "focus": int(m.group(3)),
                      "menu": int(m.group(4)), "mx": int(m.group(5)),
                      "my": int(m.group(6))}
            wins = []          # 后来的块覆盖前面的（取最新一次转储）
            continue
        m = _GW_WIN.match(line)
        if m:
            wins.append({
                "slot": int(m.group(1)), "app": int(m.group(2)),
                "x": int(m.group(3)), "y": int(m.group(4)),
                "w": int(m.group(5)), "h": int(m.group(6)),
                "hid": int(m.group(7)), "foc": int(m.group(8)),
                "max": int(m.group(9)),
                "tbx": int(m.group(10)) if m.group(10) is not None else -1,
                "tw": int(m.group(11)) if m.group(11) is not None else 0,
            })
    return header, wins


def park_mouse(qmp, x=PARK_X, y=PARK_Y, settle=0.45):
    """把鼠标停到停车位并等内核重绘完。

    必须**等**：内核主循环按帧处理鼠标移动，move_to 只是把指针移动
    过去，画面上的光标要等下一次 gw_run 迭代才被重画到位。settle
    太短会拍到"光标还在旧位置"的半成品帧。
    """
    qmp.move_to(x, y, step=64, delay=0.02)
    time.sleep(settle)


# ---- 指针位置一致性校验（探针 vs 内核）----
def split_gw_blocks(text):
    """把累积的转储文本切成一个个独立的块（每块 = 一次转储）。

    块内第一行是头（[GW] seq=... n=...），随后是 [GW] w ... 行，
    末行是 [GW] end。
    """
    blocks = []
    cur = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("[GW] seq=") or s.startswith("[GW] n="):
            if cur:
                blocks.append("\n".join(cur))
            cur = [s]
            continue
        cur.append(s)
        if s == "[GW] end":
            blocks.append("\n".join(cur))
            cur = []
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def find_gw_block(text, seq):
    """找出 seq 匹配的那一次转储文本；找不到返回 None。

    探针按 F12 后只认**自己那一份**（seq == 探针发出的 F12 次数）：
    内核忙时转储会迟到，先读到旧的那份就会拿旧状态跟当前模型比，报出
    根本不存在的"状态漂移"（2026-10-24 C8 op#75 实测过）。
    """
    for b in split_gw_blocks(text):
        hdr, _w = parse_gw_dump(b)
        if hdr is not None and hdr.get("seq", 0) == seq:
            return b
    return None


def mouse_sync(hdr, qmp, tol=0):
    """内核转储报的指针位置 vs 探针自己跟踪的位置。

    不一致 = 探针的"相对移动"基准已经漂了，此后所有 click 都点不到位。
    这不是理论风险而是实测过的坑（2026-10-24）：park_mouse() 之后又写死
    `qmp.cx, qmp.cy = 屏幕中心`，探针以为指针在 (640,512)，内核其实在
    (1200,1008)——之后每一步相对移动都错，菜单点击全落空，**内核一个
    窗口都没开**，而探针模型还以为开了两个；几十条断言一起变红却指向
    "内核坏了"，其实是探针在撒谎。

    返回 (ok, detail)；hdr 为 None 时返回 (None, "no dump")。
    """
    if hdr is None:
        return None, "no [GW] dump"
    dx = hdr["mx"] - qmp.cx
    dy = hdr["my"] - qmp.cy
    ok = abs(dx) <= tol and abs(dy) <= tol
    return ok, "kernel=(%d,%d) probe=(%d,%d) delta=(%d,%d)" % (
        hdr["mx"], hdr["my"], qmp.cx, qmp.cy, dx, dy)
