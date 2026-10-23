# -*- coding: utf-8 -*-
"""probe_chain.py - GUI 长链条组合操作回归（单操作不炸、长链才炸的 bug class）

probe_combo.py 的六个场景虽然都是组合，但每个场景内部仍是"开窗-操作-关窗"
的短链，且从未覆盖以下组合轴（真实用户会话的常态）：

  1. 双击启动（桌面图标 400ms 双击）与开始菜单 launch 去重（已开 app 走
     restore 不 spawn——泄漏会级联污染后续所有 cascade）
  2. 双击标题栏 maximize/还原 + 任务栏最小化/恢复的几何循环
     （save 几何、inner_w/h 重算、max 态禁拖）
  3. 边界拖拽 clamp（gw_create 的 -4 钳制与拖动路径的贴边钳制数值不同）
  4. 关掉 focused 窗口后的焦点去向（gw_close 只清自己，不转移焦点——
     其余窗口标题栏应由蓝变灰）
  5. gw_focused_wnd==NULL 时桌面热键 'b'/'t' 才启动（关窗链尾验证）
  6. 菜单死区（本探针目标 bug）：菜单 rect 内但非有效行的区域（典型=被
     菜单盖住的 Files 标题栏左段 x∈[12,484) y<130）按下应关闭菜单并让
     事件继续传给窗口层，而不是被吞掉——同一点在菜单外行为正常
     （probe_combo B 场景），同一标题栏两段行为不一致即 bug；从该区域
     起拖的拖动第一帧被吞，整段拖动同样失效
  7. 菜单开着点任务栏窗口按钮 = 关菜单 + 最小化/恢复（双状态叠加）
  8. 内容持久：Notepad 文字历经 最小化->恢复->最大化->还原 不丢，
     且 File->Clear 真的清（反向钉，防像素判据恒真）
  9. 双击任务栏按钮 = min + restore 闭环（tbtn_prev 边沿防护）

本探针把这些轴串成一条 20 步长链，每步钉不变量，中途不关窗（状态全程累积），
最后统一收尾。窗口模型与内核实时同步（注释标注每步的 used/focused/hidden）。

帧管理（cur/new 双帧模式）：每步只拍 1 张新图，与上一步的 cur 比对后
cur=new。沙箱对单命令的文件写入累计有阈值（~50），早期版本每步拍
pre/post 两张 + finally 删 56 个截图，第 50 次写入被 hook 中断、链尾
场景根本没跑（CH-19/20 丢失，现象"最后几条断言神秘消失"）。

判据设计（关键教训——Win10 壁纸 0x0C7BD5->0x063B66 与聚焦标题栏 #0078D7
同色系，且开始菜单顶部用户区也是同款蓝渐变，is_title_blue 在桌面/菜单
用户区都会假绿）：
  - title_blue_exact（b>=185）：只认聚焦标题栏（实测 (0,105,189)/
    (0,109,198)/(0,117,214)），桌面渐变 y=57 实测 b=173、菜单用户区同色
  - "窗口在"      -> 内容区白点（is_page_white）
  - "焦点变迁"    -> 同一窗口标题栏位置 title_blue_exact <-> is_title_gray
  - "窗口出现/消失/移动" -> 帧差；但被上层完全盖住的窗口关闭/最小化时
    帧差天然小（6%~24%），必须用像素变迁（蓝->白/白->蓝）而不是帧差
  - "菜单开关"    -> 菜单列表面板深灰(0x1B1B1B) vs 桌面蓝渐变，用 g-r 差
  - "Terminal 在" -> 黑底 b<80（桌面渐变 b=173，不会误判）

几何锚点（Win10 主题 1280x1024，任务栏 y=992..1024，桌面可用区 0..992）：
  cascade 位置 (60,51) (116,95) (172,139) (228,183)；gw_create 钳制 x+ww>1276/y+wh>988
  拖动 clamp：nx∈[0,1280-ww], ny∈[0,992-wh]（与 gw_create 的 -4 不同，贴边）
  标题栏高 12；关闭钮中心 wx+ww-7；最小化钮 wx+ww-19
  任务栏按钮从 x=48 起：tw=8+len*8+8（File Manager=112/Notepad=72/Settings=80/
    Browser=72/Terminal=80），间隔 4
  开始菜单 rect (12,15,600,975)：顶部用户区蓝渐变 y15..115，列表面板深灰
    y115..990；列表 list_y=130 row_h=61，行中心 y=130+row*61+30
    （files=0/notepad=1/settings=4/browser=8）；右侧 x484..612：
    设置 y130..203 / 电源 203..276 / 重启 276..349
  桌面图标 4 列 box=106 gap=16 x0=16 y0=21：图标0(File Manager) 中心 (69,74)
  Notepad cascade1(116,95) 内容区 (117,108)：菜单栏 ly<14（File 钮 x 119..151），
    File->Clear 项 lx∈[2,92) ly∈[62,78) -> 绝对 (140,172)
  Browser cascade1(116,95) 内容区 (117,108)，地址栏 x 144..1068 y 111..131
  Terminal cascade0(60,51) 840x640，黑底内容区 (61,64)
用法: python tests/probe_chain.py
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

from probe_combo import (Qmp, Wins, ppm_load, px, is_title_blue,  # noqa: E402
                         is_page_white, frame_diff, zone_dark,
                         alive, rm, APP_WH, TITLE_CY_OFF,
                         START_BTN, MENU_X, MENU_Y0, MENU_ROW_H)
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "chain_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()
BOOT_WAIT = 90.0

# ---- 链上几何常量 ----
DESK_BOTTOM = 992            # Win10 桌面可用区底（任务栏顶）


def is_title_gray(c):
    """非聚焦标题栏灰渐变 0x6E6E6E->0x5A5A5A 中点 ~(100,100,100)。
    桌面渐变/菜单面板/内容白都不落此区间。"""
    return 55 <= c[0] <= 145 and max(c) - min(c) <= 25


def title_blue_exact(c):
    """聚焦标题栏蓝（渐变中点，r 恒 0）。桌面壁纸/菜单用户区是同色系蓝
    渐变，实测桌面 y=57 处 (8,117,206)、菜单用户区 (8,97,173)——与标题栏
    (0,105,189) 只差 r 通道（标题栏纯 #0078D7 系 r=0，壁纸 0x0C7BD5 r=12
    经 16bpp 量化为 8+）。故 r<5 是硬判据，g/b 阈值只防灰/白误入。"""
    return c[0] < 5 and 95 <= c[1] <= 140 and 185 <= c[2] <= 235


def menu_panel_gray(c):
    """菜单列表面板 0x1B1B1B alpha245 -> ~(16,24,24)：近灰、g 不超 r+20。"""
    return c[1] < c[0] + 20 and max(c) < 90


def desk_blue(c):
    """桌面/菜单用户区蓝渐变：g 显著大于 r（列表面板深灰 g≈r 不落入）。"""
    return c[1] > c[0] + 40 and c[2] > 100


def term_light(c):
    """Terminal 内容区底色 0xF0F0F0（实测 (247,243,247)）：桌面渐变/标题栏
    都不落此区间。"""
    return min(c) > 230


def bar_dark_zone(buf, w, x0, y0, x1, y1):
    """指定区域暗像素（黑字）数——CH-12 的 Browser 在 cascade1，地址栏
    区域与 probe_combo 的 cascade0 常量不同，必须显式传区域。"""
    n = 0
    for yy in range(y0, y1):
        for xx in range(x0, x1):
            o = (yy * w + xx) * 3
            if buf[o] + buf[o + 1] + buf[o + 2] < 200:
                n += 1
    return n


def title_c(app, x, y):
    ww = APP_WH[app][0]
    return x + ww // 2, y + TITLE_CY_OFF


def main():
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
    # 帧槽：每步 1 张（cur/new 模式）+ alive 4 张。screendump 覆盖写也计入
    # 沙箱单命令文件操作阈值（~50），总数必须压在阈值内；finally 不删截图。
    S = [os.path.join(ROOT, "temp", "chain_s%d.ppm" % i).replace("\\", "/")
         for i in range(40)]
    wins = Wins()
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
        qmp.shot(S[0])
        w, h, cur = ppm_load(S[0])
        rep("desktop entered (1280x1024)", (w, h) == (1280, 1024))
        if (w, h) != (1280, 1024):
            return 1
        qmp.cx, qmp.cy = w // 2, h // 2

        slot = [0]

        def step():
            """拍一张新帧并与 cur（上一步固化的帧）比对，返回 (new, d, n)。
            调用方拿到 new 后自行 `cur = new` 固化。"""
            slot[0] += 1
            qmp.shot(S[slot[0]])
            _, _, new = ppm_load(S[slot[0]])
            d, n = frame_diff(cur, new, w, h, step=4)
            return new, d, n

        def menu_open():
            qmp.click(*START_BTN)
            time.sleep(0.6)

        def menu_row(row):
            qmp.click(MENU_X, MENU_Y0 + row * MENU_ROW_H + MENU_ROW_H // 2)
            time.sleep(2.5)

        # ================= CH-01 双击桌面图标启动 Files =================
        # 双击判定 400ms（gw_handle_mouse 图标分支）。0 窗起点。
        # cur = 桌面
        qmp.dblclick(69, 74)               # 图标0 = File Manager
        time.sleep(2.5)
        wins.spawn("files")                # 模型：cascade0 (60,51,880x640)
        new, d, n = step()
        fx, fy = title_c("files", 60, 51)  # (500,57)
        rep("CH-01: double-click desktop icon launches Files",
            d * 5 > n and is_title_blue(px(new, w, fx, fy)),
            "| diff %d/%d title=%r" % (d, n, px(new, w, fx, fy)))
        cur = new

        # ================= CH-02 菜单 launch 去重 =================
        # 菜单点 Files 行（row0）：gw_launch_app 发现已开 -> gw_restore 不 spawn。
        # 泄漏 spawn 的后果：原窗失焦变灰 + 新窗占 cascade1 污染后续所有坐标。
        menu_open()
        menu_row(0)
        new, d, n = step()
        p_title = px(new, w, fx, fy)
        rep("CH-02: menu launch of running app restores (no respawn)",
            d * 20 < n and is_title_blue(p_title),
            "| diff %d/%d title=%r (small-diff=idempotent restore / "
            "blue=restored, gray+big-diff=leaked)" % (d, n, p_title))
        cur = new

        # ================= CH-03 开 Notepad 打字 =================
        # cascade 必须=1（CH-02 若泄漏这里会变成 2，下面坐标全错——级联验证）。
        menu_open()
        menu_row(1)                          # Notepad -> cascade1 (116,95)
        wins.spawn("notepad")
        new, d, n = step()
        nx, ny = title_c("notepad", 116, 95)  # (496,101)
        rep("CH-03: notepad launched at cascade1 (no cascade leak)",
            is_title_blue(px(new, w, nx, ny)),
            "| title=%r" % (px(new, w, nx, ny),))
        cur = new
        # 点内容区聚焦 + 打 hello（打字前基线 = 刚 spawn 的帧，无字）
        qmp.click(119 + 200, 108 + 40)
        time.sleep(0.3)
        np0 = zone_dark(cur, w, 119, 123, 119 + 300, 123 + 45)
        qmp.keys("hello")
        time.sleep(0.5)
        new, d, n = step()
        np1 = zone_dark(new, w, 119, 123, 119 + 300, 123 + 45)
        rep("CH-03: typing into notepad shows chars",
            np1 > np0 + 8, "| dark %d -> %d" % (np0, np1))
        cur = new

        # ================= CH-04 双击标题栏 maximize =================
        # Notepad 标题栏中心 (496,101) 双击 -> toggle_max：铺满 (0,0,1280,992)
        qmp.dblclick(nx, ny)
        time.sleep(1.0)
        new, d, n = step()
        # max 后 Files 标题栏 (500,57) 被 Notepad 内容区盖住 -> 白
        p_cov = px(new, w, fx, fy)
        rep("CH-04: double-click title maximizes window",
            d * 3 > n and is_page_white(p_cov),
            "| diff %d/%d files_title_now=%r (white=covered)" % (d, n, p_cov))
        cur = new

        # ================= CH-05 max 态任务栏最小化 =================
        # Notepad 任务栏钮：Files(btn0 48..160) 后 btn1 164..236 -> 中心 (200,1008)
        qmp.click(200, 1008)
        time.sleep(1.5)
        new, d, n = step()
        rep("CH-05: taskbar button minimizes maximized window",
            d * 3 > n, "| diff %d/%d" % (d, n))
        cur = new

        # ================= CH-06 任务栏恢复（保持 max 几何） =================
        qmp.click(200, 1008)
        time.sleep(1.5)
        new, d, n = step()
        p_cov2 = px(new, w, fx, fy)
        rep("CH-06: taskbar button restores (still maximized geometry)",
            d * 3 > n and is_page_white(p_cov2),
            "| diff %d/%d files_title=%r" % (d, n, p_cov2))
        cur = new

        # ================= CH-07 双击标题栏还原 =================
        # max 态标题栏中心 (640,6)。还原回 save 几何 (116,95,760,640)
        qmp.dblclick(640, 6)
        time.sleep(1.0)
        new, d, n = step()
        # 还原前 (496,101) 是 max 内容区白；还原后是 cascade1 标题栏蓝
        p_back = px(new, w, nx, ny)
        rep("CH-07: double-click title restores saved geometry",
            d * 3 > n and is_title_blue(p_back),
            "| diff %d/%d np_title=%r (blue=back at cascade1)" % (d, n, p_back))
        cur = new

        # ================= CH-08 内容持久 + Clear 反向钉 =================
        # 文字历经 min/max/restore 必须还在；File->Clear 必须真清（否则上面的
        # "文字在"断言可能是渲染坏掉的恒真——反向钉保证判据有效）
        qmp.click(119 + 200, 108 + 40)       # 聚焦 Notepad 内容区
        time.sleep(0.3)
        np0 = zone_dark(cur, w, 119, 123, 119 + 300, 123 + 45)
        qmp.keys("world")
        time.sleep(0.5)
        new, d, n = step()
        np1 = zone_dark(new, w, 119, 123, 119 + 300, 123 + 45)
        rep("CH-08: notepad text survives minimize/maximize/restore",
            np1 > np0 + 8, "| dark %d -> %d" % (np0, np1))
        cur = new
        # File 菜单 -> Clear（内容区坐标 lx 2..92, ly 62..78 -> 绝对 (140,172)）
        qmp.click(135, 115)                  # File 钮（lx 2..34, ly<14）
        time.sleep(0.4)
        qmp.click(140, 172)                  # Clear 项（ly 62..78）
        time.sleep(0.8)
        new, d, n = step()
        np2 = zone_dark(new, w, 119, 123, 119 + 300, 123 + 45)
        rep("CH-08: File->Clear really clears (reverse check)",
            np2 < np1 - 8, "| dark %d -> %d" % (np1, np2))
        cur = new
        qmp.keys("world")
        time.sleep(0.5)
        new, d, n = step()
        np3 = zone_dark(new, w, 119, 123, 119 + 300, 123 + 45)
        rep("CH-08: typing works again after Clear",
            np3 > np2 + 8, "| dark %d -> %d" % (np2, np3))
        cur = new
        rep("CH-08: gui alive after max/min/restore cycle",
            alive(qmp, S, w, h, 36))

        # ================= CH-09 边界拖拽 clamp =================
        # 拖 Notepad 标题栏到 (1100,900)（出界）-> clamp nx=1276-760=516,
        # ny=992-640=352。注意与 gw_create 的 -4 钳制（1276/988）不同。
        qmp.drag(nx, ny, 1100, 900)
        time.sleep(0.8)
        wins.list[1] = ("notepad", 516, 352, 760, 640)   # 模型同步 clamp 结果
        new, d, n = step()
        cx2, cy2 = title_c("notepad", 516, 352)  # (896,358)
        p_content = px(new, w, 700, 500)     # Notepad 内容区（clamp 后位置）
        rep("CH-09: dragging past bottom-right clamps to desktop edge",
            d * 3 > n and title_blue_exact(px(new, w, cx2, cy2))
            and is_page_white(p_content),
            "| diff %d/%d title=%r content=%r" % (d, n,
                                                  px(new, w, cx2, cy2),
                                                  p_content))
        cur = new

        # ================= CH-10 clamp 后关闭 focused 窗 =================
        # Notepad 是 focused（CH-08 点击/CH-09 拖动都 bring_front）。
        # 关掉后 gw_focused_wnd=NULL、Files 失焦——Files 标题栏由白（被盖）
        # 变灰。注意 (896,358) 关窗后露出桌面渐变（与标题蓝同色系，不可判）。
        qmp.click(516 + 760 - 7, 352 + TITLE_CY_OFF)   # (1269,358)
        time.sleep(1.2)
        wins.list.pop(1)
        new, d, n = step()
        p_files_title = px(new, w, fx, fy)
        rep("CH-10: closing focused window removes it (frame diff)",
            d * 3 > n, "| diff %d/%d" % (d, n))
        rep("CH-10: remaining window loses focus (blue->gray)",
            is_title_gray(p_files_title),
            "| files_title=%r (gray=unfocused, expected)" % (p_files_title,))
        cur = new

        # ================= CH-11 无焦点热键启动 Browser =================
        # gw_focused_wnd==NULL（CH-10 关掉了 focused 窗）-> 'b' 才启动。
        # cascade = used 数 = 1（只剩 Files）-> cascade1 (116,95,1000x768)
        qmp.key("b")
        time.sleep(2.5)
        wins.spawn("browser")
        new, d, n = step()
        bx, by = title_c("browser", 116, 95)  # (616,101)
        rep("CH-11: hotkey 'b' launches Browser only when nothing focused",
            title_blue_exact(px(new, w, bx, by)),
            "| diff %d/%d br_title=%r" % (d, n, px(new, w, bx, by)))
        cur = new

        # ================= CH-12 地址栏打字 + 菜单开 Settings =================
        # Browser cascade1：地址栏内容区偏移 x+27..x+952, y+3..y+23
        # -> 绝对 x 144..1068 y 111..131（probe_combo 的 BAR 常量是 cascade0
        # 坐标，这里必须自定义区域）。基线用 CH-11 的帧（地址栏尚无输入）。
        qmp.click(150, 120)
        time.sleep(0.3)
        n1 = bar_dark_zone(cur, w, 144, 111, 1068, 131)
        qmp.keys("abc")
        time.sleep(0.5)
        new, d, n = step()
        n2 = bar_dark_zone(new, w, 144, 111, 1068, 131)
        rep("CH-12: typing into browser address bar shows chars",
            n2 > n1 + 3, "| bar_dark %d -> %d" % (n1, n2))
        cur = new
        # 菜单开 Settings（row4）-> cascade2 (172,139,680,640)
        menu_open()
        menu_row(4)
        wins.spawn("settings")
        new, d, n = step()
        sx, sy = title_c("settings", 172, 139)  # (512,145)
        rep("CH-12: settings launched at cascade2",
            title_blue_exact(px(new, w, sx, sy)),
            "| title=%r" % (px(new, w, sx, sy),))
        cur = new
        # 切窗口后地址栏必须失焦（spawn->gw_create->gw_bring_front->wv_bar_set_focus(0)）
        qmp.keys("xyz")
        time.sleep(0.5)
        new, d, n = step()
        n3 = bar_dark_zone(new, w, 144, 111, 1068, 131)
        rep("CH-12: launching app via menu clears address bar focus",
            n3 <= n2 + 2, "| bar_dark %d -> %d (expect flat)" % (n2, n3))
        cur = new

        # ================= CH-13 菜单死区（本探针目标 bug） =================
        # 菜单开着点 Files 标题栏左段 (100,57)：在菜单 rect(12,15,612,990) 内、
        # 列表区但 my=57<list_y=130 非有效行。正确行为：关菜单 + Files
        # bring_front（标题栏灰->蓝）。错误行为（修复前）：事件被吞，菜单不关、
        # 窗口无响应——同一点在菜单外（x>612）行为正常（probe_combo B 场景），
        # 同一标题栏两段行为不一致即 bug。
        # 判据注意：(500,57) 修复前被菜单用户区蓝渐变盖住（b=173 假蓝），
        # 必须用 title_blue_exact（b>=185 只认真标题栏）。
        menu_open()
        new, d, n = step()
        p_menu_before = px(new, w, 36, 500)
        qmp.click(100, 57)
        time.sleep(1.0)
        new, d, n = step()
        p_menu_after = px(new, w, 36, 500)
        p_files_focus = px(new, w, fx, fy)
        rep("CH-13: click inside menu non-interactive zone closes menu",
            menu_panel_gray(p_menu_before) and desk_blue(p_menu_after),
            "| (36,500) %r -> %r (gray=menu open / blue=closed)" %
            (p_menu_before, p_menu_after))
        rep("CH-13: same click reaches window (Files gray->blue)",
            title_blue_exact(p_files_focus),
            "| files_title=%r (blue=focused, gray/menu-blue=fail)" %
            (p_files_focus,))
        cur = new

        # ================= CH-14 菜单开着点任务栏按钮 =================
        # 菜单开着点 Files 任务栏钮（btn0 中心 (104,1008)）：
        # 应 = 关菜单（desktop_mouse 2761）+ Files 最小化。Files 880x640 被
        # Browser/Settings 大面积盖住，最小化帧差天然小（实测 11.4%）——
        # 主判据用标题栏像素变迁（focused 标题栏 r=0 蓝 -> 露出桌面渐变
        # r=8+ 蓝，r 通道是硬区分），帧差 d*10 兜底。
        menu_open()
        qmp.click(104, 1008)
        time.sleep(1.5)
        new, d, n = step()
        rep("CH-14: taskbar button click while menu open minimizes window",
            not title_blue_exact(px(new, w, fx, fy)) and d * 10 > n,
            "| diff %d/%d files_title=%r (r>=8=desktop exposed=minimized)" %
            (d, n, px(new, w, fx, fy)))
        cur = new

        # ================= CH-15 任务栏恢复 Files =================
        qmp.click(104, 1008)
        time.sleep(1.5)
        new, d, n = step()
        p_back = px(new, w, fx, fy)
        rep("CH-15: taskbar button restores minimized Files",
            title_blue_exact(p_back),
            "| diff %d/%d title=%r" % (d, n, p_back))
        cur = new

        # ================= CH-16 拖窗口跨菜单区域 =================
        # 菜单开着从 Files 标题栏左段 (100,57) 起拖到 (300,300)：
        # move_dx=40 move_dy=6 -> 落点 (260,294)。第一帧 press 落在菜单死区，
        # 修复后关菜单+起拖（CH-13 同源）。若死区未修：press 被吞、拖不动。
        # 注意 (700,320) 不能当"内容区白点"用——Files 列表选中行是强调蓝
        # (0,121,214)，实测该点落在选中行上。判据=新标题栏 r=0 蓝 + 帧差。
        menu_open()
        qmp.drag(100, 57, 300, 300)
        time.sleep(0.8)
        wins.list[0] = ("files", 260, 294, 880, 640)   # 模型同步
        new, d, n = step()
        fx2, fy2 = title_c("files", 260, 294)  # (700,300)
        rep("CH-16: dragging window starting inside menu zone works",
            d * 6 > n and title_blue_exact(px(new, w, fx2, fy2)),
            "| diff %d/%d title=%r (r=0 blue at new spot=dragged)" %
            (d, n, px(new, w, fx2, fy2)))
        cur = new

        # ================= CH-17 双击任务栏按钮 =================
        # 双击 Files 任务栏钮 = min + restore 闭环。正常时画面复原（diff 小）；
        # 若 restore 失效/位置漂移则 diff 大。反相判据 + 标题栏像素。
        qmp.dblclick(104, 1008)
        time.sleep(1.2)
        new, d, n = step()
        rep("CH-17: double-click taskbar button = minimize+restore round trip",
            d * 20 < n and title_blue_exact(px(new, w, fx2, fy2)),
            "| diff %d/%d (small=restored) title=%r" %
            (d, n, px(new, w, fx2, fy2)))
        cur = new

        # ================= CH-18 关闭链（收三窗） =================
        # 关 Settings（被 Browser 完全包含，靠"无 focused 窗口包含该点时最高
        # slot 胜出"的命中规则点到下层）：帧差天然极小（差集为空），必须用
        # 标题栏像素变迁（Settings 标题 -> 露出 Browser 内容白）。
        qmp.click(172 + 680 - 7, 139 + TITLE_CY_OFF)   # (845,145)
        time.sleep(1.2)
        new, d, n = step()
        p_st = px(new, w, sx, sy)
        rep("CH-18: closing overlapped settings (hit lower window)",
            is_page_white(p_st),
            "| diff %d/%d settings_title=%r (white=Browser page under)" %
            (d, n, p_st))
        wins.list.pop(2)
        cur = new
        # 关 Browser：标题栏 r=0 蓝 -> 露出桌面渐变（r=8+，y=101 处与标题栏
        # 几同色，只有 r 通道能硬区分）；帧差 21.3%（Browser 与 Files 大幅
        # 重叠，暴露区天然不足 1/3 屏）兜底。
        qmp.click(116 + 1000 - 7, 95 + TITLE_CY_OFF)    # (1109,101)
        time.sleep(1.2)
        new, d, n = step()
        rep("CH-18: closing browser",
            not title_blue_exact(px(new, w, bx, by)) and d * 5 > n,
            "| diff %d/%d br_title=%r" % (d, n, px(new, w, bx, by)))
        wins.list.pop(1)
        cur = new
        # 关 Files（位置来自 CH-16 拖动结果，级联验证）。桌面判据取 (1000,300)：
        # 无图标（图标只在 x<500 列）、无水印（右下角），露出=桌面蓝渐变。
        qmp.click(260 + 880 - 7, 294 + TITLE_CY_OFF)    # (1133,300)
        time.sleep(1.2)
        new, d, n = step()
        p_desk = px(new, w, 1000, 300)
        rep("CH-18: closing files leaves clean desktop",
            d * 3 > n and desk_blue(p_desk) and not is_page_white(p_desk),
            "| diff %d/%d desk=%r" % (d, n, p_desk))
        wins.list = []
        rep("CH-18: gui alive after close chain", alive(qmp, S, w, h, 38))

        # ================= CH-19 slot 回收 + 热键重启 =================
        # 全关后再按 't'：cascade 必须回到 0（slot 回收），Terminal 开在
        # cascade0 (60,51,840,640)。终端底色 0xF0F0F0 浅灰白（不是黑底），
        # 修复前（CH-18c 级联失败、Files 未关）Terminal 会开在 cascade1，
        # (480,200) 露出桌面渐变非白 -> 红。
        qmp.key("t")
        time.sleep(2.5)
        wins.spawn("terminal")
        new, d, n = step()
        tx, ty = title_c("terminal", 60, 51)   # (480,57)
        rep("CH-19: hotkey 't' relaunches after all closed (slot recycled)",
            d * 3 > n and title_blue_exact(px(new, w, tx, ty))
            and term_light(px(new, w, 480, 200)),
            "| diff %d/%d term_title=%r bg=%r" %
            (d, n, px(new, w, tx, ty), px(new, w, 480, 200)))
        cur = new

        # ================= CH-20 收尾 =================
        qmp.click(60 + 840 - 7, 51 + TITLE_CY_OFF)      # (893,57)
        time.sleep(1.2)
        new, d, n = step()
        p_desk2 = px(new, w, 1000, 300)
        rep("CH-20: closing terminal leaves clean desktop",
            d * 3 > n and desk_blue(p_desk2) and not is_page_white(p_desk2),
            "| diff %d/%d desk=%r" % (d, n, p_desk2))

        # report
        print("\n===== probe_chain results =====")
        allok = True
        for r in results:
            allok = allok and r[1]
        print("total %d assertions, %s" % (len(results),
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
        # 不删截图：screendump 覆盖写 + os.remove 都计入沙箱单命令文件操作
        # 阈值（~50），删 40 个会把链尾场景挤掉（CH-19/20 丢失，现象"最后
        # 几条断言神秘消失"）。temp/ 在 .gitignore 内，留文件无副作用。


if __name__ == "__main__":
    sys.exit(main())
