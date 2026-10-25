# -*- coding: utf-8 -*-
"""probe_fuzz.py - GUI 组合压力：确定性随机操作流 + 未覆盖组合轴定向链

probe_combo/probe_chain 都是人设计的线性链——人只能想到"应该这么用"，
真实用户是随机顺序的混合操作流，且长链里状态会累积漂移。本探针两部分：

Part A 定向链（probe_chain 仍未覆盖的组合轴，全部有精确断言）：
  A1 Alt+Tab 循环：3 窗循环切换、最小化窗被跳过、gw_focused_wnd==NULL
     时按 Alt+Tab 的起点语义（kernel start=0，从 slot1 起扫）
  A2 slot 打满：8 窗全满时第 9 个 app 的 launch 必须安全 no-op
     （gw_create 返回 NULL 不能被解引用）、已开 app 的 launch 仍走
     restore、关掉一个后第 9 个正常 spawn（cascade=used%4）
  A3 桌面空白点击恢复"最低 slot 的最小化窗"（不是最近最小化的）
  A4 主题切换：Settings 切主题后桌面/标题栏全部重绘，切回 Win10 Blue
  A6 菜单开着点任务栏按钮：应关菜单 + 最小化/恢复（旧实现只做后者，
     菜单不关——与"点菜单外桌面/菜单内"两条路径的行为不一致）

Part B 种子随机流（默认 seed=42，可用 argv 指定复现）：
  操作池全部可精确预测（launch/close/taskbar/drag/alt_tab/type/
  desktop_click/menu/dblclick-title），模型与内核同构（slots 数组 +
  focused + menu_active + hidden + maximized + save 几何）：
  - 每个 op 后：QMP query-status + 串口无 panic 标记
  - 每 8 个 op：焦点排他性——未覆盖的窗口标题栏里，有且只有 focused
    那个是 r<5 蓝（bring_front 漏清 focused 标志会当场红）
  - 每 21 个 op：轮收尾——按模型全关（先 taskbar 恢复最小化的，再自上
    而下关可见的），桌面必须露出来（(1000,300) 桌面蓝 + x>520 无标题蓝）
  - 串口 panic / 桌面不干净 / 焦点漂移 -> FAIL 并打印最近 15 步操作史

帧管理：screendump 覆盖写计入沙箱单命令文件操作阈值（~50），全程单文件
路径复用、finally 不删截图（temp/ 在 .gitignore 内）。

判据（沿用 probe_chain 实测结论）：
  - 聚焦标题栏蓝 r<5（Win10 壁纸/菜单用户区同色系但 r=8+，只差 r 通道）
  - Files 列表选中行也是 (0,121,214)——只在标题栏中心采样，不碰内容区
  - 桌面图标彩色方块（icon0 就是 0x0078D7）在 x<520——杂散蓝扫描排除
"""
import os
import random
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

from probe_combo import (Qmp, ppm_load, px, is_title_blue,  # noqa: E402
                         is_page_white, frame_diff, alive, rm,
                         APP_WH, TITLE_CY_OFF, START_BTN)
from probe_chain import (title_blue_exact, desk_blue, is_title_gray,  # noqa: E402
                         menu_panel_gray)
from gui_probe import (park_mouse, parse_gw_dump, mouse_sync,  # noqa: E402
                      split_gw_blocks, find_gw_block, PARK_X, PARK_Y)
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "fuzz_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()
BOOT_WAIT = 90.0

# ---- 屏幕/窗口几何（Win10 主题 1280x1024，任务栏 y=992..1024）----
GFX_W, GFX_H = 1280, 1024
DESK_BOTTOM = 992
TITLE_H = 12
MENU_LIST_Y = 130
MENU_ROW_H = 61
MENU_ROW_X = 248                     # 列表列中心（x<484 安全区）
TB_Y = 1008
TB_BTN_X0 = 48                       # Win10 任务栏按钮起点（GNOME=92）
TB_BTN_XMAX = GFX_W - 90             # 1190：按钮排到这里的被丢弃
# 桌面图标区（4 列 box=106 gap=16 x0=16 y0=21，13 个到 y=493）——
# 随机点击/杂散蓝扫描一律排除（icon0 就是 0x0078D7 蓝方块，
# 且大多数 app 窗标题中心 x<520，不能用"排除左半屏"当扫描判据）
ICON_BOX, ICON_GAP, ICON_X0, ICON_Y0, ICON_COLS = 106, 16, 16, 21, 4


def icon_rects():
    out = []
    for idx in range(13):
        r, c = idx // ICON_COLS, idx % ICON_COLS
        out.append((ICON_X0 + c * (ICON_BOX + ICON_GAP),
                    ICON_Y0 + r * (ICON_BOX + ICON_GAP),
                    ICON_BOX, ICON_BOX))
    return out

APP_IDX = {"files": 0, "notepad": 1, "sysinfo": 2, "clock": 3, "settings": 4,
           "about": 5, "terminal": 6, "paint": 7, "browser": 8}
APP_BY_IDX = dict((v, k) for k, v in APP_IDX.items())
# fuzz 启动池排除 settings：主题切换会改标题栏配色（r<5 判据失效）、
# 鼠标速度行会改灵敏度导致 QMP 增量移动与探针坐标模型脱钩
FUZZ_APPS = ["files", "notepad", "sysinfo", "clock", "about",
             "terminal", "paint", "browser"]

PANIC_MARKERS = ["KERNEL PANIC", "kernel panic", "PANIC:", "Triple fault",
                 "#GP ", "#PF ", "#DF ", "stack guard", "stackchk:"]

# 操作池权重（可预测操作才入池）
OP_POOL = ([("launch", 22), ("taskbar", 16), ("drag", 12), ("alt_tab", 12),
            ("close", 10), ("type", 8), ("desktop", 8), ("dbltitle", 6),
            ("menu_toggle", 4), ("menu_row", 4), ("minbtn", 8)])
NOPS = 150
ROUND_EVERY = 30
FOCUS_EVERY = 8


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


class FuzzModel(object):
    """与内核同构的窗口模型：8 个 slot + focused + menu_active。

    内核所有遍历（任务栏按钮/Alt+Tab/桌面恢复/cascade 计数）都是 slot 顺序，
    关窗释放 slot、再开窗取最低空 slot——所以模型必须按 slot 存，不能按
    spawn 顺序存（probe_combo 的 Wins 按 spawn 顺序，关窗后再开会错位）。
    """

    def __init__(self):
        self.slots = [None] * 8
        self.focused = None      # slot 或 None
        self.menu = False

    # ---- 查询 ----
    def used(self):
        return [s for s in range(8) if self.slots[s]]

    def visible(self):
        return [s for s in range(8) if self.slots[s] and not self.slots[s]["hidden"]]

    def find_app(self, idx):
        for s in range(8):
            w = self.slots[s]
            if w and APP_IDX.get(w["app"]) == idx:
                return s
        return None

    def topmost_at(self, x, y):
        """命中测试：包含该点的可见窗口中，focused 优先，否则最高 slot。"""
        cand = [s for s in self.visible()
                if self.slots[s]["x"] <= x < self.slots[s]["x"] + self.slots[s]["w"]
                and self.slots[s]["y"] <= y < self.slots[s]["y"] + self.slots[s]["h"]]
        if not cand:
            return None
        if self.focused in cand:
            return self.focused
        return max(cand)

    def covered(self, s):
        """窗口 s 的标题栏中心是否被别的可见窗盖住。绘制序：非聚焦按 slot
        序先画、聚焦最后画；命中测试 focused 优先——两者一致，所以 focused
        窗永远在最上（它的标题栏永远可见），只有非聚焦窗才可能被盖住。"""
        if s == self.focused:
            return False
        w = self.slots[s]
        cx, cy = self.title_c(s)
        for s2 in self.visible():
            if s2 == s:
                continue
            w2 = self.slots[s2]
            if not (s2 > s or s2 == self.focused):
                continue
            if w2["x"] <= cx < w2["x"] + w2["w"] and w2["y"] <= cy < w2["y"] + w2["h"]:
                return True
        return False

    def title_c(self, s):
        w = self.slots[s]
        return w["x"] + w["w"] // 2, w["y"] + TITLE_CY_OFF

    def close_pt(self, s):
        w = self.slots[s]
        return w["x"] + w["w"] - 7, w["y"] + TITLE_CY_OFF

    def taskbar_buttons(self):
        """任务栏按钮 = 所有 used 窗（含最小化）按 slot 排，超宽丢弃。"""
        out = []
        bx = TB_BTN_X0
        for s in self.used():
            app = self.slots[s]["app"]
            t = APP_WH[app][2] if app in APP_WH else "?"
            tw = min(16 + 8 * len(t), 180)
            if bx + tw > TB_BTN_XMAX:
                break
            out.append((s, bx, tw))
            bx += tw + 4
        return out

    def sync_from_kernel(self, wins):
        """把模型整体重置成内核转储里的真实状态。

        用于真值清理之后重建基线：残留/漂移被清掉后，模型必须回到与
        内核一致的起点，否则下一轮场景的 slot/几何全错。
        非应用窗口（app=99）模型里没有对应项 -> 用 "?" 占位（宽度按
        未知标题算；清理路径走内核真值 tbx，不依赖模型算的任务栏位置）。
        """
        self.slots = [None] * 8
        self.focused = None
        self.menu = False
        for w in wins:
            app = APP_BY_IDX.get(w["app"], "?")
            self.slots[w["slot"]] = {
                "app": app, "x": w["x"], "y": w["y"], "w": w["w"],
                "h": w["h"], "hidden": bool(w["hid"]), "max": bool(w["max"]),
                "sx": w["x"], "sy": w["y"], "sw": w["w"], "sh": w["h"]}
            if w["foc"]:
                self.focused = w["slot"]

    # ---- 状态迁移（与内核函数一一对应）----
    def spawn(self, app):
        cascade = len(self.used())
        ww, wh, _ = APP_WH[app]
        # 内核 gw_create 的**最小尺寸钳制**（模型漏了这条 → Paint 窗口
        # 高度 179 被内核抬到 gw_scale_y(90)=192，拖到底部时钳制出的 y
        # 差 13px，标题栏采样点随之偏掉，焦点排他断言假红）：
        #   if (w < gw_scale_x(120)) w = gw_scale_x(120);
        #   if (h < gw_scale_y(90))  h = gw_scale_y(90);
        ww = max(ww, GFX_W * 120 // 640)
        wh = max(wh, GFX_H * 90 // 480)
        x = 60 + (cascade % 4) * 56
        # cascade y 步进真相：gw_spawn_app 用 gw_scale_y(22)=1024*22/480=46
        # （probe_combo 以来一直误写 44；标题栏 12px 高时 ±2px 仍落渐变里
        # 所以历史全绿，cascade=3 时偏 6px 会正好踩聚焦窗顶部 1px 亮边）
        y = 51 + (cascade % 4) * 46
        if x + ww > 1276:
            x = 1276 - ww
        if y + wh > 988:
            y = 988 - wh
        slot = next(i for i in range(8) if not self.slots[i])
        self.slots[slot] = {"app": app, "x": x, "y": y, "w": ww, "h": wh,
                            "hidden": False, "max": False,
                            "sx": x, "sy": y, "sw": ww, "sh": wh}
        self.focused = slot          # gw_create -> gw_bring_front
        return slot

    def close(self, s):
        self.slots[s] = None
        if self.focused == s:
            self.focused = None

    def minimize(self, s):
        self.slots[s]["hidden"] = True
        if self.focused == s:
            self.focused = None

    def restore(self, s):
        self.slots[s]["hidden"] = False
        self.focused = s             # gw_restore -> gw_bring_front

    def bring_front(self, s):
        self.focused = s

    def toggle_max(self, s):
        w = self.slots[s]
        if not w["max"]:
            w["sx"], w["sy"], w["sw"], w["sh"] = w["x"], w["y"], w["w"], w["h"]
            w["x"], w["y"] = 0, 0
            w["w"], w["h"] = GFX_W, DESK_BOTTOM
            w["max"] = True
        else:
            w["x"], w["y"] = w["sx"], w["sy"]
            w["w"], w["h"] = w["sw"], w["sh"]
            w["max"] = False
        self.focused = s

    def launch(self, app):
        """gw_launch_app：已开 -> restore；未开且有空位 -> spawn；满 -> no-op。"""
        idx = APP_IDX[app]
        s = self.find_app(idx)
        if s is not None:
            self.restore(s)
            return "restore slot%d" % s
        if len(self.used()) < 8:
            return "spawn slot%d" % self.spawn(app)
        return "noop(full)"

    def alt_tab(self):
        start = self.focused if self.focused is not None else 0
        for k in range(1, 9):
            s = (start + k) % 8
            w = self.slots[s]
            if w and not w["hidden"]:
                if s != self.focused:
                    self.focused = s
                return
        # 全不可见：focus 不变

    def desktop_click(self):
        """桌面空白按下：恢复最低 slot 的最小化窗（kernel 2934-2941）。"""
        for s in range(8):
            if self.slots[s] and self.slots[s]["hidden"]:
                self.restore(s)
                return "restore slot%d" % s
        return "noop"


class Fuzzer(object):
    def __init__(self, qmp, model, S, shots, log=None):
        self.qmp = qmp
        self.m = model
        self.S = S
        self.shots = shots          # 截图计数（沙箱阈值 ~50）
        # 串口日志路径：各探针各用各的（probe_combo2 用 combo2_serial.log）。
        # 写死模块级 LOG 会让 combo2 读 fuzz 的日志 -> 永远读不到转储。
        self.log = log or LOG
        self.results = []
        self.history = []           # 操作描述史
        self.serial_pos = 0
        self.w = GFX_W
        self.h = GFX_H
        # 开机空桌面参考帧：assert_desk 拿它做逐像素比对判定"无残留"。
        # 老实现只扫蓝色标题栏 -> 非聚焦窗的**灰**标题扫不出来 -> 残留窗口
        # 不可见 -> 后续场景的 slot/几何全线错位（2026-10-24 实测）。
        self.ref = None
        # 内核转储的序号对账：探针每按一次 F12 就期待 seq 递增 1，
        # 只认**自己那一份**转储（内核忙时转储会迟到，读到旧的会报出
        # 根本不存在的"状态漂移"——2026-10-24 C8 op#75 实测）。
        self.f12_seq = 0
        self.gw_text = ""          # 累积的 [GW] 转储行
        # 上一个拖过的窗口 slot：op_drag 用它排除"连续两次拖同一窗"——
        # 那会踩内核 400ms 双击窗口被误判成 toggle_max（见 op_drag 注释）
        self.last_drag_wnd = None

    def rep(self, name, ok, detail=""):
        self.results.append((name, ok, detail))
        print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                           (" | " + detail) if detail else ""), flush=True)

    # ---------- 串口 / 存活 ----------
    def _pump_serial(self):
        """读串口新增内容；[GW] 行另外累积到 self.gw_text。

        累积而不丢弃：check_fast（panic 巡检）和 gw_state（取转储）共享
        同一个串口文件，谁先读到都算"已读"，但转储不能被 panic 巡检吃掉
        ——它要留给对账用。
        """
        try:
            with open(self.log, "rb") as f:
                f.seek(self.serial_pos)
                data = f.read()
                self.serial_pos = f.tell()
        except OSError:
            return ""
        txt = data.decode("latin-1", "replace")
        for line in txt.splitlines():
            if line.strip().startswith("[GW]"):
                self.gw_text += line + "\n"
        return txt

    def serial_new(self):
        return self._pump_serial()

    def check_fast(self, tag):
        """每 op 后：QEMU 存活 + 串口无 panic 标记。返回 False=必须中止。"""
        try:
            r = self.qmp.cmd("query-status")
            running = r.get("return", {}).get("status") == "running"
        except Exception as e:
            running = False
            r = str(e)
        txt = self.serial_new()
        bad = [k for k in PANIC_MARKERS if k in txt]
        if not running or bad:
            self.rep("%s: qemu alive / no panic" % tag, False,
                     "| running=%s markers=%r tail=%r" %
                     (running, bad, txt[-200:]))
            return False
        return True

    # ---------- 像素断言 ----------
    def shot(self, idx):
        """取一帧。**先停鼠标到停车位再 screendump**：
        内核把 16x16 软件光标直接画进 LFB（gw_cursor_draw），光标落在
        采样点上就会读到光标图形而不是真实内容（实测 (496,103) 读到
        (0,0,0) 黑色，被误判成"标题栏不是蓝"）。停车位在任务栏空白
        区，远离所有标题栏/内容区采样点。
        """
        park_mouse(self.qmp)
        self.qmp.shot(self.S[idx])
        self.shots[0] += 1
        _, _, buf = ppm_load(self.S[idx])
        return buf

    # ---------- 内核状态对账（机器判定，不靠读码推理）----------
    def gw_state(self, timeout=3.0):
        """按 F12 让内核把真实窗口状态转储到串口，返回 (header, windows)。

        内核侧用 serial_write **直写 COM1**——GUI 模式下
        terminal_writestring 已被 g_gfx_hook 重定向进 GUI 终端窗口
        缓冲，ezos_console_write 的输出永远不进串口。

        **只认自己那一份转储**：内核每次转储带自增 seq，探针知道自己发了
        第几次 F12。内核忙/串口缓冲慢时转储会迟到，若不校验 seq 就可能拿
        "上一次请求的旧转储"跟当前模型比 → 报出根本不存在的状态漂移
        （2026-10-24 C8 op#75 实测：模型刚拖完的位置 vs 上一拍旧转储）。
        """
        self.f12_seq += 1
        want = self.f12_seq
        self.qmp.hmc("sendkey f12")
        t0 = time.time()
        while True:
            time.sleep(0.2)
            self._pump_serial()
            blk = find_gw_block(self.gw_text, want)
            if blk is not None:
                return parse_gw_dump(blk)
            if time.time() - t0 >= timeout:
                break
        # 超时：退回最后一份转储，并把 seq 不匹配显式打出来（别让调用方
        # 拿旧状态当新状态用）
        blocks = split_gw_blocks(self.gw_text)
        if blocks:
            hdr, wins = parse_gw_dump(blocks[-1])
            print("   [gw] seq mismatch: want=%d got=%r" %
                  (want, hdr.get("seq") if hdr else None), flush=True)
            return hdr, wins
        return None, []

    def check_model_quiet(self, tag):
        """轻量对账：模型 vs 内核转储，**不一致才记断言**。

        长链每一步都用它（一致就静默，否则 120 步会刷出 240 条断言把
        真正的失败淹掉）；不一致时额外打印最近的 op 序列——状态漂移类
        bug 必须知道"从哪一步开始分家"，否则只看到结果对不上、定位不到
        原因。
        """
        hdr, wins = self.gw_state()
        if hdr is None:
            return True
        self.m.menu = bool(hdr["menu"])
        mine = []
        for s in self.m.used():
            w = self.m.slots[s]
            mine.append({"slot": s, "app": APP_IDX[w["app"]], "x": w["x"],
                         "y": w["y"], "w": w["w"], "h": w["h"],
                         "hid": 1 if w["hidden"] else 0,
                         "foc": 1 if s == self.m.focused else 0,
                         "max": 1 if w["max"] else 0})
        fields = ("slot", "app", "x", "y", "w", "h", "hid", "foc", "max")
        ok = len(mine) == len(wins)
        detail = "model=%r kernel=%r" % (mine, wins)
        if ok:
            for a, b in zip(mine, wins):
                bad = [(k, a[k], b[k]) for k in fields if a[k] != b[k]]
                if bad:
                    ok = False
                    detail = "slot%d diff %r" % (a["slot"], bad)
                    break
        if not ok:
            self.rep("%s: model matches kernel state" % tag, False,
                     "| " + detail)
            print("   [chain] 最近操作: %r" % (self.history[-14:],), flush=True)
        return ok

    def assert_model_matches_kernel(self, tag):
        """逐字段比对探针模型与内核真实窗口状态。

        这是把"模型错了"和"内核错了"分开的关键：一旦两者漂移，后续
        所有像素断言的前提都不可信（2026-10-24 C7 场景踩过）。
        """
        hdr, wins = self.gw_state()
        if hdr is None:
            self.rep("%s: kernel state dump available" % tag, False,
                     "| no [GW] dump in serial (F12 not delivered?)")
            return False
        # 菜单状态顺手按内核真值同步（转储里 menu= 就是真值）：
        # 模型的 m.menu 一旦与内核不同步，ensure_menu_closed() 会做出
        # 相反的动作（以为开着其实关着 / 反之），下一步点击就落空。
        self.m.menu = bool(hdr["menu"])
        exp = self.m
        mine = []
        for s in exp.used():
            w = exp.slots[s]
            mine.append({"slot": s, "app": APP_IDX[w["app"]], "x": w["x"],
                         "y": w["y"], "w": w["w"], "h": w["h"],
                         "hid": 1 if w["hidden"] else 0,
                         "foc": 1 if s == exp.focused else 0,
                         "max": 1 if w["max"] else 0})
        # 只比模型知道的字段：内核转储还带 tbx/tw（任务栏按钮真值），
        # 模型算不出（内核上限 gw_scale_x(180)=360，模型历史写 180），
        # 拿它比会把"字段集合不同"误判成状态不一致。
        fields = ("slot", "app", "x", "y", "w", "h", "hid", "foc", "max")
        ok = len(mine) == len(wins)
        detail = "model=%r kernel=%r" % (mine, wins)
        if ok:
            for a, b in zip(mine, wins):
                bad = [(k, a[k], b[k]) for k in fields if a[k] != b[k]]
                if bad:
                    ok = False
                    detail = "slot%d diff %r" % (a["slot"], bad)
                    break
        self.rep("%s: model matches kernel state" % tag, ok, "| " + detail)
        # 指针位置一致性：探针跟踪的坐标必须等于内核实际位置，否则后续
        # 所有点击都点不到位（模型与内核"同时错"，模型对账会假绿）
        sync_ok, sync_detail = mouse_sync(hdr, self.qmp)
        self.rep("%s: probe pointer matches kernel pointer" % tag,
                 sync_ok is True, "| " + sync_detail)
        return ok

    def assert_focus_is(self, tag, slot):
        """焦点排他性：未覆盖窗口的标题栏中，有且只有 slot 是 r<5 蓝。"""
        # 开始菜单面板盖住 x<612（fuzz op#80 实测：菜单开着时 files 标题
        # 中心 (500,57) 落进菜单用户区蓝 (8,97,173)，r=8 不计蓝 -> 假
        # 阴性 blues=[]）。菜单是桌面层浮层，关掉它不改变窗口状态。
        self.ensure_menu_closed()
        buf = self.shot(30)
        blues = []
        for s in self.m.visible():
            if self.m.covered(s):
                continue
            cx, cy = self.m.title_c(s)
            if title_blue_exact(px(buf, self.w, cx, cy)):
                blues.append(s)
        expect = [slot] if slot is not None else []
        self.rep("%s: focus exclusivity (only slot%s blue)" % (tag, slot),
                 blues == expect,
                 "| blues=%r expect=%r focused=%r" % (blues, expect, self.m.focused))

    def assert_title(self, tag, slot, pred, pname):
        self.ensure_menu_closed()   # 同上：菜单浮层会盖住标题栏采样点
        buf = self.shot(31)
        cx, cy = self.m.title_c(slot)
        c = px(buf, self.w, cx, cy)
        self.rep("%s: slot%d title %s" % (tag, slot, pname), pred(c),
                 "| (%d,%d)=%r" % (cx, cy, c))

    def assert_desk(self, tag):
        buf = self.shot(32)
        p = px(buf, self.w, 1000, 300)
        # 杂散标题蓝扫描（全宽，但排除桌面图标矩形——icon0 就是 0x0078D7；
        # 桌面渐变 r=8+ 不落入 r<5，任务栏 y>=992 不扫）
        icons = icon_rects()
        stray = 0
        diff = 0
        ref = self.ref
        for y in range(4, DESK_BOTTOM - 4, 6):
            row = y * self.w
            for x in range(4, GFX_W - 4, 6):
                if any(ix <= x < ix + iw and iy <= y < iy + ih
                       for ix, iy, iw, ih in icons):
                    continue
                o = (row + x) * 3
                c = (buf[o], buf[o + 1], buf[o + 2])
                if c[0] < 5 and 95 <= c[1] <= 140 and 185 <= c[2] <= 235:
                    stray += 1
                # 与开机空桌面参考帧逐像素比：任何差异都是残留窗口/浮层
                # （灰色标题、灰边框、灰内容底都盖得住，蓝色扫不到）
                if ref is not None and bytes(buf[o:o + 3]) != bytes(ref[o:o + 3]):
                    diff += 1
        self.rep("%s: desktop clean (no window residue)" % tag,
                 desk_blue(p) and stray == 0 and diff == 0,
                 "| desk=%r stray_blue=%d ref_diff=%d" % (p, stray, diff))

    # ---------- 操作（QMP 执行 + 模型迁移必须同步）----------
    def ensure_menu_closed(self):
        if self.m.menu:
            self.qmp.click(*START_BTN)
            time.sleep(0.6)
            self.m.menu = False

    def ensure_menu_open(self):
        """点开始钮把菜单打开，并**以内核转储为准**（menu=1）。

        开始钮是 **toggle**：菜单若已经开着，再点一下反而把它**关掉**。
        探针模型记的是"我以为我开了"，一旦与内核不同步（上一轮清理遗留
        菜单、或者点开始钮那一下没送达），随后的菜单行点击就落在空桌面
        上，窗口一个都开不出来，而模型以为开好了——2026-10-24
        combo2_final 整轮几十条断言一起红却指向"内核坏了"，根因就在这
        里。这里点一次没开就再点一次，直到内核说 menu=1。
        """
        for _ in range(2):
            self.qmp.click(*START_BTN)
            time.sleep(0.6)
            hdr, _w = self.gw_state()
            if hdr is not None and hdr["menu"] == 1:
                self.m.menu = True
                return True
        self.m.menu = False
        return False

    def op_launch(self, app=None):
        if app is None:
            app = random.choice(FUZZ_APPS)
        idx = APP_IDX[app]
        # 热键路径只在无焦点时可用（'t'/'b'），否则走菜单
        if self.m.focused is None and app in ("terminal", "browser"):
            self.qmp.key(app[0])
            time.sleep(1.8)
            desc = self.m.launch(app)
        else:
            self.ensure_menu_closed()
            self.ensure_menu_open()
            self.qmp.click(MENU_ROW_X, MENU_LIST_Y + idx * MENU_ROW_H + MENU_ROW_H // 2)
            time.sleep(1.8)
            self.m.menu = False
            desc = self.m.launch(app)
        return "launch(%s)=%s" % (app, desc)

    def op_close(self):
        cands = []
        for s in self.m.visible():
            cx, cy = self.m.close_pt(s)
            if self.m.topmost_at(cx, cy) == s:
                cands.append(s)
        if not cands:
            return "close=skip(none clickable)"
        # 排除刚按过标题栏的窗（400ms 双击窗口，见 op_drag 注释）
        if self.last_drag_wnd is not None:
            cands = [c for c in cands if c != self.last_drag_wnd]
        if not cands:
            return "close=skip(last title-bar window)"
        s = self.m.focused if self.m.focused in cands else max(cands)
        self.last_drag_wnd = s
        cx, cy = self.m.close_pt(s)
        self.ensure_menu_closed()
        self.qmp.click(cx, cy)
        time.sleep(1.0)
        self.m.close(s)
        self.last_drag_wnd = None   # 窗已销毁：slot 可能被回收复用
        return "close(slot%d)" % s

    def op_taskbar(self):
        btns = self.m.taskbar_buttons()
        if not btns:
            return "taskbar=skip"
        s, bx, tw = random.choice(btns)
        self.qmp.click(bx + tw // 2, TB_Y)
        time.sleep(1.2)
        w = self.m.slots[s]
        if self.m.focused == s and not w["hidden"]:
            self.m.minimize(s)
            act = "minimize"
        else:
            self.m.restore(s)
            act = "restore"
        self.m.menu = False      # 菜单开着点任务栏也应关菜单（A6 钉死）
        return "taskbar(slot%d %s)=%s" % (s, w["app"], act)

    def op_drag(self):
        self.ensure_menu_closed()
        cands = []
        for s in self.m.visible():
            w = self.m.slots[s]
            for _ in range(8):
                x0 = random.randint(w["x"] + 30, w["x"] + w["w"] - 30)
                y0 = random.randint(w["y"], w["y"] + TITLE_H - 1)
                if self.m.topmost_at(x0, y0) == s:
                    cands.append((s, x0, y0))
                    break
        # ★ 排除上一个拖过的窗口：连续两次拖同一窗标题栏，第二次 press 会
        #   踩内核的 400ms 双击窗口 -> gw_toggle_max -> 窗口突然铺满全屏，
        #   模型却以为只是拖了位置（2026-10-25 fuzz 42d op#40 实测：
        #   'drag(slot0 files -> 19,352)' 紧跟 'drag(slot0 files -> 0,352)'
        #   -> max 0->1，下游 20+ 条断言全红）。
        #   这不是内核 bug：双击最大化是**正确**语义，真机上两次 press 间隔
        #   由人的手决定；QEMU TCG 下 GUI 任务 CPU 饥饿、事件处理延迟可达
        #   数百 ms（dbg_dclick.py 量到 dt=201/399 tick vs wall 2500ms），
        #   两次本无关联的 press 会在 tick 时间上落进双击窗口。
        if self.last_drag_wnd is not None:
            cands = [c for c in cands if c[0] != self.last_drag_wnd]
        if not cands:
            return "drag=skip"
        s, x0, y0 = random.choice(cands)
        self.last_drag_wnd = s
        w = self.m.slots[s]
        x1 = random.randint(0, GFX_W - 1)
        y1 = random.randint(0, DESK_BOTTOM - 1)
        self.qmp.drag(x0, y0, x1, y1)
        time.sleep(0.6)
        self.m.bring_front(s)      # 标题栏按下先 bring_front（2968）
        if not w["max"]:           # max 态禁拖（2988）
            nx = clamp(w["x"] + (x1 - x0), 0, GFX_W - w["w"])
            ny = clamp(w["y"] + (y1 - y0), 0, DESK_BOTTOM - w["h"])
            w["x"], w["y"] = nx, ny
        return "drag(slot%d %s -> %d,%d)" % (s, w["app"], w["x"], w["y"])

    def op_minbtn(self):
        """标题栏最小化按钮：点的是标题栏右二按钮（x+w-20）。

        与任务栏钮语义**不同**：任务栏钮对非聚焦窗是"恢复置顶"，标题栏
        最小化钮走 gw_title_btn_hit 早返回，不经过 gw_bring_front——所以
        非聚焦窗被最小化时**焦点不变**（聚焦窗被最小化则焦点置 NULL）。
        """
        cands = []
        for s in self.m.visible():
            w = self.m.slots[s]
            x, y = w["x"] + w["w"] - 20, w["y"] + TITLE_CY_OFF
            if self.m.topmost_at(x, y) == s:
                cands.append(s)
        # 同上：上一个刚按过标题栏的窗不再按（标题栏按钮也走 pressed 沿，
        # 400ms 内第二次按下同一窗会被判双击 -> toggle_max）
        if self.last_drag_wnd is not None:
            cands = [c for c in cands if c != self.last_drag_wnd]
        if not cands:
            return "minbtn=skip"
        s = random.choice(cands)
        self.last_drag_wnd = s
        w = self.m.slots[s]
        x, y = w["x"] + w["w"] - 20, w["y"] + TITLE_CY_OFF
        self.ensure_menu_closed()
        self.qmp.click(x, y)
        time.sleep(1.0)
        self.m.minimize(s)
        return "minbtn(slot%d %s)" % (s, w["app"])

    def op_alt_tab(self):
        self.qmp.hmc("sendkey alt-tab")
        time.sleep(0.5)
        before = self.m.focused
        self.m.alt_tab()
        return "alt_tab(%s -> %s)" % (before, self.m.focused)

    def op_type(self):
        if self.m.focused is None:
            return "type=skip(no focus, hotkeys!)"
        s = self.m.focused
        n = random.randint(1, 4)
        txt = "".join(random.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(n))
        self.qmp.keys(txt)
        time.sleep(0.4)
        return "type(%s)->slot%d %r" % (self.m.slots[s]["app"], s, txt)

    def op_desktop(self):
        for _ in range(30):
            x = random.randint(620, GFX_W - 10)   # 避开图标区与菜单 rect(..612)
            y = random.randint(10, DESK_BOTTOM - 10)
            if self.m.topmost_at(x, y) is None:
                break
        else:
            return "desktop=skip(no empty point)"
        self.ensure_menu_closed()
        self.qmp.click(x, y)
        time.sleep(0.8)
        # 菜单已关：点中窗口 -> bring_front；空白 -> 恢复最低 slot 最小化窗
        t = self.m.topmost_at(x, y)
        if t is not None:
            self.m.bring_front(t)
            return "desktop_click(%d,%d)->focus slot%d" % (x, y, t)
        return "desktop_click(%d,%d)->%s" % (x, y, self.m.desktop_click())

    def op_dbltitle(self):
        self.ensure_menu_closed()
        cands = []
        for s in self.m.visible():
            w = self.m.slots[s]
            for _ in range(8):
                x0 = random.randint(w["x"] + 30, w["x"] + w["w"] - 30)
                y0 = random.randint(w["y"], w["y"] + TITLE_H - 1)
                t = self.m.topmost_at(x0, y0)
                if t is not None:
                    tw = self.m.slots[t]
                    if tw["y"] <= y0 < tw["y"] + TITLE_H:
                        cands.append((t, x0, y0))
                    break
        if not cands:
            return "dbltitle=skip"
        s, x0, y0 = random.choice(cands)
        self.last_drag_wnd = s     # dblclick 也按了两次标题栏：同样要防
        self.qmp.dblclick(x0, y0)
        time.sleep(1.0)
        self.m.toggle_max(s)
        return "dbltitle(slot%d %s) max=%s" % (s, self.m.slots[s]["app"],
                                               self.m.slots[s]["max"])

    def op_menu_toggle(self):
        self.qmp.click(*START_BTN)
        time.sleep(0.6)
        hdr, _w = self.gw_state()
        self.m.menu = bool(hdr and hdr["menu"])
        return "menu_toggle -> %s" % self.m.menu

    def op_menu_row(self):
        hdr, _w = self.gw_state()
        if hdr is None or not hdr["menu"]:
            self.m.menu = False
            return "menu_row=skip(menu closed)"
        i = random.randint(0, 8)     # 只点应用行 0..8，绝不碰 9/10/11（退出/重启/关机）
        self.qmp.click(MENU_ROW_X, MENU_LIST_Y + i * MENU_ROW_H + MENU_ROW_H // 2)
        time.sleep(1.8)
        self.m.menu = False
        app = [k for k, v in APP_IDX.items() if v == i][0]
        return "menu_row(%d %s)=%s" % (i, app, self.m.launch(app))

    def do_op(self, kind):
        fn = getattr(self, "op_" + kind)
        desc = fn()
        self.history.append(desc)
        if len(self.history) > 200:
            self.history = self.history[-200:]
        return desc

    # ---------- 轮收尾：按模型全关 + 验桌面 ----------
    # ---------- 真值驱动的清理（不依赖探针模型）----------
    def cleanup_by_kernel(self, tag, max_rounds=40):
        """按**内核真值**清空所有窗口，并把模型同步到内核状态。

        为什么必须真值驱动：探针模型一旦与内核漂移（残留窗 / 模型算错
        cascade），按模型算出来的关闭钮坐标就会点空——点了没关、模型却
        以为关了 -> 残窗留在屏上 -> 后面所有场景的 slot/几何全错位
        （2026-10-24 C7 实测：内核多出一个 880x640 的 Files 窗，clock 被
        spawn 到 slot2/(172,143) 而非模型以为的 slot0/(60,51)）。

        每轮读一次 F12 转储（机器判定，不靠读码推理）：
          1) menu=1 -> 点开始钮关菜单。菜单面板 x12..612 会盖住大量窗口
             的关闭钮，不先关掉就永远点不中。
          2) 有聚焦且可见的窗 -> 点它的关闭钮。聚焦窗最后画、命中测试也
             优先它 -> 关闭钮一定在最上层，点得到。
          3) 否则 -> 点某个窗的任务栏按钮（tbx+tw/2）：隐藏窗点了 =
             restore+bring_front；可见非聚焦窗点了同样是 restore+bring_front。
        直到转储的 n == 0（并断言真的清干净了）。
        """
        ok = True
        for _ in range(max_rounds):
            hdr, wins = self.gw_state()
            if hdr is None:
                self.rep("%s: kernel state available for cleanup" % tag, False,
                         "| no [GW] dump in serial")
                return False
            # **菜单必须先于 n==0 处理**：开始菜单开着时"没有窗口"也是
            # 合法状态，老实现先 break → 菜单留在屏上，而 sync_from_kernel
            # 把模型的 m.menu 置 False —— 模型说菜单关了、内核说开着。
            # 下一轮 launch() 点开始钮是 toggle：这一下反而把菜单**关掉**，
            # 随后的菜单行点击落在空桌面上，窗口一个都开不出来，而模型
            # 以为开好了（2026-10-24 combo2_final 实测 C2/C3/C4 启动全灭、
            # C5/C6/C7 时好时坏，几十条断言一起红却指向"内核坏了"）。
            if hdr["menu"]:
                self.qmp.click(*START_BTN)
                time.sleep(0.6)
                continue
            if hdr["n"] == 0:
                break
            foc = None
            for w in wins:
                if w["foc"]:
                    foc = w
                    break
            if foc is not None and not foc["hid"]:
                self.qmp.click(foc["x"] + foc["w"] - 7,
                               foc["y"] + TITLE_CY_OFF)
                time.sleep(0.8)
                continue
            cand = None
            for w in wins:
                if w["tbx"] >= 0:
                    cand = w
                    break
            if cand is None:
                ok = False
                break
            self.qmp.click(cand["tbx"] + cand["tw"] // 2, TB_Y)
            time.sleep(0.8)
        hdr, wins = self.gw_state()
        clean = hdr is not None and hdr["n"] == 0
        # 收尾还必须把开始菜单关掉：留着它，下一轮 toggle 语义就反了
        if hdr is not None and hdr["menu"]:
            self.qmp.click(*START_BTN)
            time.sleep(0.6)
            hdr, wins = self.gw_state()
            clean = clean and hdr is not None and hdr["n"] == 0 and not hdr["menu"]
        self.rep("%s: kernel has no window left after cleanup" % tag,
                 clean and ok,
                 "| n=%r menu=%r wins=%r" % (hdr["n"] if hdr else None,
                                            hdr["menu"] if hdr else None, wins))
        if clean:
            self.m.sync_from_kernel(wins)
        return clean and ok

    def round_end(self, tag):
        self.ensure_menu_closed()
        guard = 0
        # 1) taskbar 恢复所有最小化窗（按钮含最小化窗，slot 序）
        while any(self.m.slots[s] and self.m.slots[s]["hidden"]
                  for s in self.m.used()) and guard < 20:
            guard += 1
            hid = [s for s in self.m.used() if self.m.slots[s]["hidden"]]
            btns = dict((s, (bx, tw)) for s, bx, tw in self.m.taskbar_buttons())
            if hid[0] not in btns:
                break
            bx, tw = btns[hid[0]]
            self.qmp.click(bx + tw // 2, TB_Y)
            time.sleep(1.0)
            self.m.restore(hid[0])
        # 2) 逐个关窗：**总是关当前最上的窗**（聚焦的，否则 slot 号最大的）
        #    ——聚焦窗最后画、命中也优先它，所以它的关闭钮一定点得到；非聚焦
        #    时 slot 号大的后画、也在最上。
        #    老实现按"关闭点是否被别的窗盖住"过滤候选，8 窗重叠时会一个候
        #    选都选不出 -> 直接 break，**残窗留在屏上**（assert_desk 又扫不
        #    到灰标题）-> 后面所有场景的 slot/几何全线错位（2026-10-24 实测）
        guard = 0
        while self.m.used() and guard < 24:
            guard += 1
            vis = self.m.visible()
            if not vis:
                break
            top = self.m.focused if self.m.focused in vis else max(vis)
            cx, cy = self.m.close_pt(top)
            self.qmp.click(cx, cy)
            time.sleep(1.0)
            self.m.close(top)
        # 3) 真值兜底：模型可能已与内核漂移，按内核真值再清一遍
        #    （并把模型同步成内核状态），保证下一轮从同一基线出发
        self.cleanup_by_kernel(tag + " (truth fallback)")
        # 4) 桌面必须干净
        self.assert_desk(tag)
        if self.m.used():
            self.rep("%s: model empty after close-all" % tag, False,
                     "| residue=%r" % ([(s, self.m.slots[s]["app"])
                                        for s in self.m.used()],))
        return not self.m.used()


def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 42
    random.seed(seed)
    results = []

    def rep(name, ok, detail=""):
        results.append((name, ok, detail))
        print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                           (" | " + detail) if detail else ""), flush=True)

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
    S = [os.path.join(ROOT, "temp", "fuzz_s%d.ppm" % i).replace("\\", "/")
         for i in range(40)]
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
        # 开机参考帧也必须在**停车位**拍：后续所有 assert_desk 都拿它做
        # 逐像素 ref_diff，光标位置不同 = 参考帧与当前帧差一片
        # （2026-10-24 实测：ref 在 (640,512) 拍、当前帧光标在别处，
        #  ref_diff 恒不为 0，"桌面干净"断言全红）。
        park_mouse(qmp)
        w, h, ref_buf = ppm_load(qmp.shot(S[0]))
        shots[0] += 1
        rep("desktop entered (1280x1024)", (w, h) == (1280, 1024))
        if (w, h) != (1280, 1024):
            return 1
        # **不要**在这里写死 qmp.cx/qmp.cy：park_mouse 之后真实指针在
        # (PARK_X,PARK_Y)，谎报成屏幕中心会让此后所有相对移动全错
        # （2026-10-24 实测：内核指针一路漂到 (1279,1023)，菜单点击全
        #  落空、窗口一个没开，几十条断言一起变红却指向"内核坏了"）。
        # park_mouse 内部的 move_to 已经把 qmp.cx/cy 更新成真实位置。

        m = FuzzModel()
        f = Fuzzer(qmp, m, S, shots)
        f.rep = rep
        f.ref = ref_buf        # 开机空桌面参考帧：残留检测用

        # 开机即校验指针位置（探针跟踪 vs 内核实际）：基准一旦漂了，后面
        # 每一次点击都点不到位，几十条断言会一起变红却指向"内核坏了"
        hdr0, _ = f.gw_state()
        sync_ok, sync_detail = mouse_sync(hdr0, qmp)
        rep("pointer position matches kernel after boot",
            sync_ok is True, "| " + sync_detail)

        def focus_check(tag, slot=None):
            """焦点/标题断言的统一入口：**先**做模型-内核对账（0 截图，
            只发 F12），**再**做像素判定。

            顺序很重要：像素红了时，只有先确认"模型与内核一致"，才能
            下结论说"内核画错了"；否则红的是模型自己的锅（2026-10-24
            op#40 就是模型把 Paint 高度写成 179，内核钳到 192，拖到底
            部时 y 差 13px，标题采样点偏掉 -> 假红）。"""
            f.assert_model_matches_kernel(tag)
            if slot is None:
                slot = m.focused
            f.assert_focus_is(tag, slot)

        def menu_open():
            """点开始钮开菜单，**以内核转储 menu=1 为准**。

            开始钮是 toggle：菜单若已经开着，这一下反而把它关掉，随后
            的菜单行点击落在空桌面上，窗口一个都开不出来而模型以为开好
            了（2026-10-24 combo2_final 整轮几十条断言一起红的根因）。
            点一次没开就再点一次，直到内核说 menu=1。
            """
            for _ in range(2):
                qmp.click(*START_BTN)
                time.sleep(0.6)
                hdr, _w = f.gw_state()
                if hdr is not None and hdr["menu"] == 1:
                    m.menu = True
                    return True
            m.menu = False
            return False

        def menu_row(i):
            qmp.click(MENU_ROW_X, MENU_LIST_Y + i * MENU_ROW_H + MENU_ROW_H // 2)
            time.sleep(1.8)
            m.menu = False

        def launch_via_menu(app):
            menu_open()
            menu_row(APP_IDX[app])
            return m.launch(app)

        # ================= A1 Alt+Tab 循环 =================
        launch_via_menu("files")       # slot0 (60,51)
        launch_via_menu("notepad")     # slot1 (116,95)
        launch_via_menu("terminal")    # slot2 (172,139)
        focus_check("A1: 3 windows spawned", m.focused)
        # alt-tab: terminal(slot2) -> files(slot0)（k=6 绕回）
        qmp.hmc("sendkey alt-tab")
        time.sleep(0.5)
        m.alt_tab()
        focus_check("A1: alt-tab wraps terminal->files", m.focused)
        f.assert_title("A1: files focused after wrap", 0, title_blue_exact, "blue")
        # alt-tab: files(slot0) -> notepad(slot1)
        qmp.hmc("sendkey alt-tab")
        time.sleep(0.5)
        m.alt_tab()
        focus_check("A1: alt-tab files->notepad", m.focused)
        f.assert_title("A1: notepad focused", 1, title_blue_exact, "blue")
        # 最小化 notepad：它此刻是 focused（刚 alt-tab files->notepad）——
        # 任务栏钮对 focused 窗就是最小化，点一次即可（"先恢复再最小化"的
        # 两次点法只适用于非焦点窗，A3 的 files 才是那个场景）
        qmp.click(200, TB_Y)           # notepad btn1 (164..236)
        time.sleep(1.2)
        m.minimize(1)
        # alt-tab: focused=None -> start=0，跳过隐藏的 slot1 -> terminal(slot2)
        qmp.hmc("sendkey alt-tab")
        time.sleep(0.5)
        m.alt_tab()
        focus_check("A1: alt-tab from NULL focus skips hidden", m.focused)
        f.assert_title("A1: terminal focused (hidden notepad skipped)", 2,
                       title_blue_exact, "blue")
        f.assert_title("A1: minimized notepad title gone", 1,
                       lambda c: not is_title_gray(c), "not-gray")
        f.round_end("A1 round")
        if not f.check_fast("A1"):
            return 1

        # ================= A2 slot 打满 =================
        for app in ["files", "notepad", "sysinfo", "clock", "about",
                    "terminal", "paint", "settings"]:
            launch_via_menu(app)
        focus_check("A2: 8 slots full (settings last)", m.focused)
        # 已开 app 的 launch 仍走 restore（不 spawn）
        menu_open()
        menu_row(0)
        r = m.launch("files")
        rep("A2a: duplicate launch restores (no spawn when full)",
            r.startswith("restore"), "| %s" % r)
        focus_check("A2a: files restored+focused", m.focused)
        # 第 9 个 app：slot 满 -> gw_create 返回 NULL -> 安全 no-op，
        # 焦点不变（若泄漏 spawn，files 会失焦变灰、cascade 被污染）
        menu_open()
        menu_row(8)
        r = m.launch("browser")
        rep("A2b: 9th app launch is safe no-op when full",
            r == "noop(full)", "| %s" % r)
        focus_check("A2b: focus unchanged after no-op launch", m.focused)
        # 关掉 focused 的 files 释放 slot0
        cx, cy = m.close_pt(0)
        qmp.click(cx, cy)
        time.sleep(1.0)
        m.close(0)
        # 第 9 个 app 现在能 spawn：cascade=7 used -> (228,183)
        menu_open()
        menu_row(8)
        r = m.launch("browser")
        rep("A2c: browser spawns after slot freed", r.startswith("spawn"),
            "| %s" % r)
        focus_check("A2c: browser focused at cascade7", m.focused)
        f.round_end("A2 round")
        if not f.check_fast("A2"):
            return 1

        # ================= A3 桌面空白恢复最低 slot 最小化窗 =================
        launch_via_menu("files")       # slot0
        launch_via_menu("notepad")     # slot1
        # 两个都最小化：launch notepad 后 focused=slot1，files 非焦点——
        # files 先点一次=恢复置顶，再点一次=最小化；notepad 同理两次
        qmp.click(104, TB_Y)          # files btn0 [48,160) 中心
        time.sleep(1.2)
        m.restore(0)
        qmp.click(104, TB_Y)
        time.sleep(1.2)
        m.minimize(0)
        qmp.click(200, TB_Y)          # notepad btn1 [164,236) 中心
        time.sleep(1.2)
        m.restore(1)
        qmp.click(200, TB_Y)
        time.sleep(1.2)
        m.minimize(1)
        rep("A3: both windows minimized",
            m.visible() == [] and len(m.used()) == 2, "| used=%r" % (m.used(),))
        # 点桌面空白 -> 恢复最低 slot 的 files（不是最近最小化的 notepad）
        qmp.click(1000, 300)
        time.sleep(1.0)
        act = m.desktop_click()
        rep("A3a: desktop click restores lowest-slot hidden window",
            act == "restore slot0", "| %s" % act)
        focus_check("A3a: files restored (not notepad)", m.focused)
        f.assert_title("A3a: notepad still hidden (title not gray)", 1,
                       lambda c: not is_title_gray(c), "not-gray")
        # 再点一次 -> 恢复剩下的 notepad
        qmp.click(1000, 300)
        time.sleep(1.0)
        act = m.desktop_click()
        rep("A3b: second desktop click restores notepad",
            act == "restore slot1", "| %s" % act)
        focus_check("A3b: notepad restored+focused", m.focused)
        f.assert_title("A3b: notepad title blue", 1, title_blue_exact, "blue")
        f.round_end("A3 round")
        if not f.check_fast("A3"):
            return 1

        # ================= A4 主题切换 =================
        launch_via_menu("settings")    # slot0 (60,51,680x640)
        launch_via_menu("files")       # slot1 (60,51,880x640) 盖住 settings
        # settings 被 files 盖住：点 taskbar btn0 恢复置顶=聚焦
        qmp.click(88, TB_Y)
        time.sleep(1.2)
        m.restore(0)
        focus_check("A4: settings focused via taskbar", m.focused)
        # 切 Win10 Contrast（row4）：强调色 0x000000 -> 标题栏黑、桌面白
        qmp.click(231, 175)            # settings 内容区 row4 中心
        time.sleep(1.0)
        buf = f.shot(33)
        p_t = px(buf, w, 400, 57)      # settings 标题栏中心（渐变中段）
        p_c = px(buf, w, 700, 300)     # settings 内容空白（rows 区 y<=246 以下）
        # Contrast: accent=0x000000 -> 标题渐变黑->黑、内容底 gw_bg_rgb=白。
        # 桌面墙纸是硬编码 Win10 渐变（不随主题），不断言桌面。
        rep("A4a: switch to Contrast theme repaints (black title/white content)",
            max(p_t) < 60 and min(p_c) > 250,
            "| title=%r content=%r" % (p_t, p_c))
        # 切回 Win10 Blue（row0）
        qmp.click(231, 103)
        time.sleep(1.0)
        buf = f.shot(34)
        p_t2 = px(buf, w, 400, 57)
        p_d2 = px(buf, w, 1000, 300)
        rep("A4b: switch back to Win10 Blue (blue title/blue bg)",
            title_blue_exact(p_t2) and desk_blue(p_d2),
            "| title=%r desk=%r" % (p_t2, p_d2))
        focus_check("A4b: focus intact after theme switch", m.focused)
        f.round_end("A4 round")
        if not f.check_fast("A4"):
            return 1

        # ================= A6 菜单开着点任务栏按钮 =================
        launch_via_menu("files")       # slot0
        menu_open()
        buf = f.shot(35)
        p_menu = px(buf, w, 36, 500)
        rep("A6: start menu opened (panel gray)", menu_panel_gray(p_menu),
            "| (36,500)=%r" % (p_menu,))
        # 菜单开着点 files 任务栏钮：应关菜单 + files 最小化
        qmp.click(104, TB_Y)
        time.sleep(1.4)
        m.menu = False
        m.minimize(0)
        buf = f.shot(36)
        p_menu2 = px(buf, w, 36, 500)
        p_ft = px(buf, w, 500, 57)
        rep("A6a: taskbar click while menu open closes menu too",
            desk_blue(p_menu2),
            "| (36,500)=%r (gray=still open=BUG)" % (p_menu2,))
        rep("A6a: same click minimizes files",
            not title_blue_exact(p_ft) and not is_title_gray(p_ft),
            "| files_title=%r" % (p_ft,))
        # 再点任务栏钮：恢复 files（菜单此时应已关）
        qmp.click(104, TB_Y)
        time.sleep(1.4)
        m.restore(0)
        focus_check("A6b: taskbar click restores files", m.focused)
        f.assert_title("A6b: files title blue after restore", 0,
                       title_blue_exact, "blue")
        f.round_end("A6 round")
        if not f.check_fast("A6"):
            return 1

        # ================= Part B 种子随机流 =================
        kinds = []
        for k, wt in OP_POOL:
            kinds.extend([k] * wt)
        print("\n----- fuzz seed=%d ops=%d -----" % (seed, NOPS), flush=True)
        aborted = False
        for i in range(NOPS):
            kind = random.choice(kinds)
            try:
                desc = f.do_op(kind)
            except Exception as e:
                rep("fuzz op#%d %s raised" % (i, kind), False, "| %r" % (e,))
                aborted = True
                break
            if not f.check_fast("fuzz op#%d %s" % (i, desc)):
                aborted = True
                break
            # 每步轻量对账（只发 F12，0 截图）：不一致才记断言，并打印
            # 最近 14 条操作序列，用来定位分歧**起始于哪一步**——否则只能
            # 看到"op#40 起 max 0->1"这种下游症状，归因全靠猜。
            f.check_model_quiet("fuzz op#%d" % (i + 1))
            if (i + 1) % FOCUS_EVERY == 0:
                # 先对账模型与内核（0 张截图，只发 F12）：像素断言红了时
                # 才能分清是"内核画错了"还是"模型漂了"（2026-10-24 op#40
                # 焦点排他红过一次，必须机器判定而不是猜）
                focus_check("fuzz op#%d" % (i + 1), m.focused)
            if (i + 1) % ROUND_EVERY == 0:
                if not f.round_end("fuzz round@%d" % (i + 1)):
                    aborted = True
                    break
                if not f.check_fast("fuzz round@%d" % (i + 1)):
                    aborted = True
                    break
        if not aborted:
            f.round_end("fuzz final")
            rep("fuzz: gui alive after %d ops" % NOPS,
                alive(qmp, S, w, h, 37))

        # report
        print("\n===== probe_fuzz results (seed=%d) =====" % seed)
        allok = True
        for r in results:
            allok = allok and r[1]
        print("total %d assertions, %s" % (len(results),
                                           "ALL PASS" if allok else "HAS FAILURES"))
        if not allok:
            print("last ops: %r" % (f.history[-15:],))
        print("screendumps used: %d (sandbox budget ~50)" % shots[0])
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
        # 不删截图：screendump 覆盖写 + os.remove 都计入沙箱单命令文件操作
        # 阈值（~50）。temp/ 在 .gitignore 内，留文件无副作用。


if __name__ == "__main__":
    sys.exit(main())
