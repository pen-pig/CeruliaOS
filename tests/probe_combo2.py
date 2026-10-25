# -*- coding: utf-8 -*-
"""probe_combo2.py - GUI 长链组合场景（补 probe_chain/probe_combo/fuzz 的空档）

三个已有探针的分工：
  probe_chain  人设计的线性链（~20 步），每步都有断言
  probe_combo   人设计的组合轴（单场景为主）
  probe_fuzz    种子随机压力（顺序随机、状态可预测），但每 21 步
                **重置一次**（轮收尾全关窗）——等价于 4 段 21 步短链

本探针补的空档：
  1) probe_fuzz 的链被轮收尾切成短段，"状态累积漂移"这类 bug 抓不到；
     C8 是 **60 步中间不重置** 的真长链
  2) 组合轴没覆盖的：标题栏最小化按钮、窗口重叠的命中/绘制序一致性、
     最大化窗经过"最小化→恢复"后的几何、非聚焦窗被关时的焦点归属、
     菜单与窗口标题栏的叠加

场景（判据全部是**像素/几何/焦点语义**，不拿模型状态比自己）：
  C1 标题栏最小化按钮（gw_title_btn_hit 的 btn==1 路径）
     C1a 聚焦窗点标题栏最小化钮 -> 隐藏且焦点置 NULL（整屏无蓝标题）
     C1b **非聚焦**窗点标题栏最小化钮 -> 隐藏但**焦点不变**
         （任务栏路径没有这条：最小化钮走 gw_title_btn_hit 早返回，
           不经过 gw_bring_front，焦点语义与任务栏路径不同）
  C2 窗口重叠：把 A 拖到 B 头上
     - 绘制序（聚焦窗最后画）与命中序（聚焦窗优先命中）必须一致
     - 点重叠处：焦点不能翻到被盖的窗上（A 的标题必须仍是蓝）
     - Alt+Tab 反转后：被盖窗的标题栏应变灰、聚焦窗应变蓝
  C3 最大化 -> 任务栏最小化 -> 任务栏恢复：恢复后**仍是最大化几何**
     （gw_minimize/gw_restore 都不动 maximized/x/y/w/h；判据用
       "标题栏横跨全屏宽度" = (1000,6) 是不是标题蓝）
  C4 双击标题栏 max/restore 几何守恒 + **三连击不连触发**
     （内核 last_title_wnd=NULL 防三击连触发，必须钉死）
  C5 菜单开着点窗口标题栏（菜单 rect 之外）-> 菜单关 + 该窗获焦
  C6 全部窗口最小化后 Alt+Tab -> 焦点保持 NULL（整屏无蓝标题）
  C7 关掉**非聚焦**窗 -> 焦点不变；slot 释放后新开窗取**最低空 slot**
     且 cascade 按当前 used 数重算（位置随之前移）
  C8 长链 60 步（中途不重置）：每 10 步焦点排他 + 每步无 panic + 末尾桌面干净

判据说明：
  - 标题蓝 r<5（Win10 壁纸/菜单用户区 r=8+，只差 r 通道）
  - 非聚焦标题栏是灰渐变 0x6E6E6E->0x5A5A5A（is_title_gray）
  - 窗口内容底色 gw_bg_rgb = 0xF3F3F3 (243,243,243)：既不是标题蓝
    也不是标题灰，可用来判定"这一像素被窗体盖住了"

调试：PROBE_DUMP=1 时每个场景后打印屏幕上所有标题栏带（复用已有截图
缓冲，不额外消耗 screendump 预算），用于核对"模型位置 vs 内核实际位置"。
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

from probe_combo import (Qmp, ppm_load, px, alive, rm,  # noqa: E402
                         TITLE_CY_OFF, START_BTN)
from probe_chain import (title_blue_exact, desk_blue, is_title_gray,  # noqa: E402
                         menu_panel_gray)
from probe_fuzz import (FuzzModel, Fuzzer, GFX_W, GFX_H, DESK_BOTTOM,  # noqa: E402
                        TITLE_H, MENU_LIST_Y, MENU_ROW_H, MENU_ROW_X, TB_Y,
                        APP_IDX, BOOT_WAIT, clamp)
from gui_probe import (park_mouse, parse_gw_dump, mouse_sync,  # noqa: E402
                      PARK_X, PARK_Y)
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "combo2_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()
DUMP = os.environ.get("PROBE_DUMP") == "1"

# 标题栏按钮位置（gw_title_btn_hit）：
#   close : [x+w-12, x+w-1)   -> 点 x+w-7
#   min   : [x+w-25, x+w-14)  -> 点 x+w-20
CLOSE_DX = -7
MIN_DX = -20
# 长链步数（中途不重置）：60 -> 90 -> 120，状态累积漂移类 bug 需要更长的链
CHAIN_OPS = 120


def title_min_pt(w):
    return (w["x"] + w["w"] + MIN_DX, w["y"] + TITLE_CY_OFF)


def title_close_pt(w):
    return (w["x"] + w["w"] + CLOSE_DX, w["y"] + TITLE_CY_OFF)


def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 7
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
    # 帧槽：screendump 覆盖写也计入沙箱单命令文件操作阈值（~50），
    # 全程总调用数压在 40 以内；finally 不删截图（temp/ 在 .gitignore 内）。
    # 33 个槽：Fuzzer 的 assert_* 固定用 30/31/32。
    S = [os.path.join(ROOT, "temp", "combo2_s%d.ppm" % i).replace("\\", "/")
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
        # 开机参考帧也必须在停车位拍（光标画进 LFB，位置不同 = ref_diff
        # 恒不为 0）——与 probe_fuzz 同一处理
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
        f = Fuzzer(qmp, m, S, shots, log=LOG)   # 用本探针自己的串口日志
        f.rep = rep
        f.ref = ref_buf        # 开机空桌面参考帧：残留检测用

        # 开机即校验指针位置（探针跟踪 vs 内核实际）：基准一旦漂了，后面
        # 每一次点击都点不到位，几十条断言会一起变红却指向"内核坏了"
        hdr0, _ = f.gw_state()
        sync_ok, sync_detail = mouse_sync(hdr0, qmp)
        rep("pointer position matches kernel after boot",
            sync_ok is True, "| " + sync_detail)

        def shot(idx):
            """取一帧（idx 循环复用槽位，避免文件数膨胀）。

            **先停鼠标到停车位**：内核 gw_cursor_draw 把 16x16 软件光标
            直接画进 LFB，screendump 会拍到——采样点落在光标上就读到
            光标图形（实测 (496,103) 读到 (0,0,0) 黑色，被误判成
            "标题栏不是蓝"）。
            """
            park_mouse(qmp)
            shots[0] += 1
            _, _, buf = ppm_load(qmp.shot(S[idx % len(S)]))
            return buf

        def kernel_state():
            """F12 让内核把真实窗口状态转储到串口（机器判定用）。"""
            hdr, wins = f.gw_state()
            return hdr, wins

        def model_vs_kernel(tag):
            """逐字段比对探针模型与内核真实状态。

            这是把"探针模型错"与"内核真错"分开的唯一可靠手段：像素
            截图分不清"窗口没了"和"窗口还在但失焦了"（2026-10-24
            C7 场景踩过：模型说 notepad 已关，屏幕上它还在）。
            """
            hdr, wins = kernel_state()
            if hdr is None:
                rep("%s: kernel state dump" % tag, False,
                    "| no [GW] dump in serial")
                return False
            mine = []
            for s in m.used():
                w = m.slots[s]
                mine.append({"slot": s, "app": APP_IDX[w["app"]], "x": w["x"],
                             "y": w["y"], "w": w["w"], "h": w["h"],
                             "hid": 1 if w["hidden"] else 0,
                             "foc": 1 if s == m.focused else 0,
                             "max": 1 if w["max"] else 0})
            # 只比模型知道的字段（内核转储另带 tbx/tw 任务栏按钮真值）
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
            rep("%s: model matches kernel state" % tag, ok, "| " + detail)
            sync_ok, sync_detail = mouse_sync(hdr, qmp)
            rep("%s: probe pointer matches kernel pointer" % tag,
                sync_ok is True, "| " + sync_detail)
            return ok

        def chain_check(tag):
            """长链每步的**轻量**模型-内核对账（只发 F12，0 截图）。

            不一致才记断言（一致就静默），否则 120 步会刷出 240 条断言把
            真正的失败淹掉；不一致时额外打印最近的 op 序列——状态漂移类
            bug 必须知道"是哪一步之后开始分家"。
            """
            return f.check_model_quiet(tag)

        def bands(buf):
            """屏幕上所有标题栏带（蓝=聚焦 / 灰=非聚焦），用于定位窗口。"""
            out = []
            cur = None
            for y in range(DESK_BOTTOM):
                xs = []
                row = y * w * 3
                for x in range(0, w, 4):
                    o = row + x * 3
                    r, g, b = buf[o], buf[o + 1], buf[o + 2]
                    if r < 5 and 95 <= g <= 140 and 185 <= b <= 235:
                        xs.append(x)
                    elif 55 <= r <= 145 and max(r, g, b) - min(r, g, b) <= 25:
                        xs.append(x)
                if len(xs) > 20:
                    if cur and cur[1] == y - 1:
                        cur[1] = y
                        cur[2] = min(cur[2], xs[0])
                        cur[3] = max(cur[3], xs[-1])
                    else:
                        if cur:
                            out.append(cur)
                        cur = [y, y, xs[0], xs[-1]]
                else:
                    if cur:
                        out.append(cur)
                        cur = None
            if cur:
                out.append(cur)
            return out

        def dump(tag, buf):
            if not DUMP:
                return
            print("  [dump %s] model=%r focus=%r" %
                  (tag, [(s, m.slots[s]["app"], m.slots[s]["x"], m.slots[s]["y"],
                          m.slots[s]["w"], m.slots[s]["h"], m.slots[s]["hidden"])
                         for s in m.used()], m.focused), flush=True)
            for (y0, y1, x0, x1) in bands(buf):
                print("    band y=%d..%d x=%d..%d px(%d,%d)=%r" %
                      (y0, y1, x0, x1, (x0 + x1) // 2, (y0 + y1) // 2,
                       px(buf, w, (x0 + x1) // 2, (y0 + y1) // 2)), flush=True)

        def menu_open():
            """点开始钮开菜单，并**用内核转储确认**菜单真的开了。

            不能只看探针自己的 m.menu：开始钮是 **toggle**，点击前菜单若
            已经开着（上一轮残留），这次点击会把它**关掉**——探针照样把
            m.menu 置 True，接下来点菜单行就落在空桌面上，窗口一个都开不
            出来，而探针模型以为开好了（2026-10-24 combo2_final 实测：
            C2/C3/C4 的启动全灭、C5/C6/C7 又时好时坏，全屏几十条断言一起
            红却指向"内核坏了"）。这里以**内核真值**为准：点一次没开就再
            点一次，直到转储的 menu=1。
            """
            for _ in range(2):
                qmp.click(*START_BTN)
                time.sleep(0.6)
                hdr, _w = kernel_state()
                if hdr is not None and hdr["menu"] == 1:
                    m.menu = True
                    return True
            m.menu = False
            return False

        def menu_row(i):
            qmp.click(MENU_ROW_X, MENU_LIST_Y + i * MENU_ROW_H + MENU_ROW_H // 2)
            time.sleep(1.8)
            m.menu = False

        def launch(app):
            """菜单启动一个 app，并**用内核转储确认**窗口真的建出来了。

            这一条断言的价值：把"菜单没开/菜单行没点中/窗口没建"三种
            失败收敛到一处，后面所有场景断言的前提才是可信的。
            """
            ok = menu_open()
            if not ok:
                rep("launch %s: start menu opened" % app, False,
                    "| kernel menu != 1（点开始钮没打开菜单）")
            menu_row(APP_IDX[app])
            desc = m.launch(app)
            hdr, wins = kernel_state()
            want = APP_IDX[app]
            got = [w for w in wins if w["app"] == want]
            rep("launch %s: kernel window created" % app, len(got) >= 1,
                "| kernel=%r" % ([(w["slot"], w["app"], w["x"], w["y"])
                                 for w in wins],))
            return desc

        def tb_btn(slot):
            d = dict((s, (bx, tw)) for s, bx, tw in m.taskbar_buttons())
            return d[slot]

        def cleanup_all():
            """按**内核真值**清空所有窗口（不依赖探针模型）。

            为什么不能按模型关：模型一旦与内核漂移（残留窗 / 模型算错
            cascade），按模型算出来的关闭钮坐标就会点空——点了没关、
            模型却以为关了 → 残窗留在屏上 → 后面所有场景的 slot/几何
            全线错位（2026-10-24 C7 实测：内核多出一个 880x640 的 Files
            窗，clock 被 spawn 到 slot2/(172,143) 而非 slot0/(60,51)，
            C7 的三条断言全灭）。旧实现按"关闭点是否被别的窗盖住"过滤
            候选，8 窗重叠时会一个候选都选不出 → 直接 break → 残窗留屏。

            现在走 F12 转储拿内核真值（slot/x/y/w/h/tbx/tw）：先关菜单
            （菜单面板 x12..612 会盖住大量窗口的关闭钮），再点**聚焦窗**
            的关闭钮（聚焦窗最后画、命中测试也优先它 → 点得到）；没有
            聚焦可见窗就点任务栏把一个窗恢复+置顶。直到转储 n==0，
            再把模型同步成内核状态，保证下一场景从同一基线出发。

            不拍桌面帧：round_end 里的 assert_desk 每轮 1 张截图，7 轮
            就是 7 张——沙箱单命令文件操作阈值 ~50，这里把预算留给
            真正的行为断言（干净与否由"内核 n==0"这条转储断言兜底）。
            """
            return f.cleanup_by_kernel("cleanup")

        def triple(x, y):
            """三连击（按下间隔 < 400ms，落在内核双击判定窗口内）。"""
            qmp.move_to(x, y)
            for _ in range(3):
                qmp.hmc("mouse_button 1")
                time.sleep(0.06)
                qmp.hmc("mouse_button 0")
                time.sleep(0.06)

        # ================= C1 标题栏最小化按钮 =================
        # C1a：聚焦窗点标题栏最小化钮 -> 隐藏 + 焦点置 NULL
        launch("files")                       # slot0 (60,51,880x640)
        mx, my = title_min_pt(m.slots[0])
        qmp.click(mx, my)
        time.sleep(1.0)
        m.minimize(0)
        buf = shot(1)
        dump("C1a after titlebar-min", buf)
        c = px(buf, w, 500, 57)
        rep("C1a: focused window hidden after titlebar-min",
            not title_blue_exact(c) and not is_title_gray(c),
            "| (500,57)=%r (desktop shows through)" % (c,))
        f.assert_desk("C1a: no title left on screen")   # 整屏杂散蓝扫描
        # 任务栏恢复
        bx, tw = tb_btn(0)
        qmp.click(bx + tw // 2, TB_Y)
        time.sleep(1.2)
        m.restore(0)
        f.assert_focus_is("C1a: restored via taskbar", m.focused)
        f.assert_title("C1a: files title blue after restore", 0, title_blue_exact, "blue")

        # C1b：非聚焦窗点标题栏最小化钮 -> 隐藏但焦点不变
        launch("notepad")                     # slot1 (116,97,760x640) 覆盖 files
        # files 的标题栏 (y 51..63) 没被 notepad(y>=97) 盖住；最小化钮
        # x=60+880-20=920 也不在 notepad(x 116..876) 内 -> 命中 files
        mx, my = title_min_pt(m.slots[0])
        qmp.click(mx, my)
        time.sleep(1.0)
        m.minimize(0)
        buf = shot(2)
        dump("C1b after titlebar-min non-focused", buf)
        c_np = px(buf, w, 496, 103)           # notepad 标题中心
        c_fl = px(buf, w, 500, 57)            # files 标题中心（已隐藏）
        rep("C1b: non-focused window minimized without stealing focus",
            title_blue_exact(c_np),
            "| notepad_t=%r (blue=焦点仍在 notepad)" % (c_np,))
        rep("C1b: hidden non-focused window leaves desktop behind",
            not title_blue_exact(c_fl) and not is_title_gray(c_fl),
            "| files_t=%r" % (c_fl,))
        model_vs_kernel("C1 end")
        cleanup_all()

        # ================= C2 窗口重叠：命中序 vs 绘制序 =================
        launch("sysinfo")                     # slot0 (60,51,680x554)
        launch("notepad")                     # slot1 (116,97,760x640) 聚焦
        # 把 sysinfo 拖到 notepad 头上：sysinfo(80,88) -> 其窗体盖住 notepad
        # 的标题栏中心 (496,103)（sysinfo 标题栏只有 y 89..100，103 落在窗体）
        qmp.drag(90, 57, 110, 94)
        time.sleep(0.8)
        m.bring_front(0)
        m.slots[0]["x"] = clamp(60 + (110 - 90), 0, GFX_W - 680)
        m.slots[0]["y"] = clamp(51 + (94 - 57), 0, DESK_BOTTOM - 554)
        rep("C2: dragged sysinfo over notepad (geometry)",
            (m.slots[0]["x"], m.slots[0]["y"]) == (80, 88),
            "| sysinfo=%r" % ((m.slots[0]["x"], m.slots[0]["y"]),))
        buf = shot(3)
        dump("C2 after drag", buf)
        c_np = px(buf, w, 496, 103)           # notepad 标题中心 -> 被窗体盖
        c_si = px(buf, w, 420, 94)            # sysinfo 标题中心 -> 自己的蓝
        rep("C2a: focused window drawn on top (notepad title hidden by body)",
            not title_blue_exact(c_np) and not is_title_gray(c_np)
            and title_blue_exact(c_si),
            "| notepad_t=%r sysinfo_t=%r" % (c_np, c_si))
        # 点重叠处：命中必须与绘制序一致（聚焦窗优先），焦点不能翻到被盖窗
        qmp.click(496, 103)
        time.sleep(0.8)
        buf = shot(4)
        dump("C2 after overlap click", buf)
        c_si3 = px(buf, w, 420, 94)           # sysinfo 标题中心
        c_np3 = px(buf, w, 496, 103)          # notepad 标题中心（被窗体盖）
        rep("C2b: click on overlap keeps focused window focused",
            title_blue_exact(c_si3) and not title_blue_exact(c_np3),
            "| sysinfo_t=%r notepad_t=%r" % (c_si3, c_np3))
        # Alt+Tab 反转：notepad 聚焦 -> 它最后画 -> 盖回 sysinfo
        qmp.hmc("sendkey alt-tab")
        time.sleep(0.6)
        m.alt_tab()
        buf = shot(5)
        dump("C2 after alt-tab", buf)
        c_si2 = px(buf, w, 420, 94)           # sysinfo 标题中心 -> 非聚焦灰
        c_np2 = px(buf, w, 496, 103)          # notepad 标题中心 -> 蓝
        rep("C2c: after alt-tab the top window covers (others turn gray)",
            is_title_gray(c_si2) and title_blue_exact(c_np2),
            "| sysinfo_t=%r notepad_t=%r" % (c_si2, c_np2))
        model_vs_kernel("C2 end")
        cleanup_all()

        # ================= C3 最大化 -> 最小化 -> 恢复（几何守恒）=========
        launch("files")                       # slot0
        qmp.dblclick(300, 57)                 # 双击标题栏 -> 最大化
        time.sleep(0.8)
        m.toggle_max(0)
        buf = shot(6)
        dump("C3 after maximize", buf)
        c = px(buf, w, 1000, 6)               # 最大化标题栏横跨全屏
        rep("C3a: double-click title maximizes (title spans full width)",
            title_blue_exact(c), "| (1000,6)=%r" % (c,))
        # 任务栏最小化
        bx, tw = tb_btn(0)
        qmp.click(bx + tw // 2, TB_Y)
        time.sleep(1.2)
        m.minimize(0)
        buf = shot(7)
        c = px(buf, w, 1000, 6)
        rep("C3b: maximized window minimized from taskbar",
            not title_blue_exact(c), "| (1000,6)=%r" % (c,))
        # 任务栏恢复 -> 必须仍是最大化几何（几何没被最小化改掉）
        qmp.click(bx + tw // 2, TB_Y)
        time.sleep(1.2)
        m.restore(0)
        buf = shot(8)
        c = px(buf, w, 1000, 6)
        rep("C3c: restore keeps maximized geometry",
            title_blue_exact(c), "| (1000,6)=%r" % (c,))
        model_vs_kernel("C3 end")
        cleanup_all()

        # ================= C4 双击几何守恒 + 三连击保护 =================
        launch("files")                       # slot0 (60,51)
        qmp.dblclick(300, 57)
        time.sleep(0.8)
        m.toggle_max(0)
        buf = shot(9)
        c = px(buf, w, 1000, 6)
        rep("C4a: maximize via double-click", title_blue_exact(c),
            "| (1000,6)=%r" % (c,))
        qmp.dblclick(300, 6)                  # 最大化后标题栏在 y 0..12
        time.sleep(0.8)
        m.toggle_max(0)
        buf = shot(10)
        c = px(buf, w, 1000, 6)
        rep("C4b: restore returns to original geometry",
            not title_blue_exact(c) and
            (m.slots[0]["x"], m.slots[0]["y"]) == (60, 51),
            "| (1000,6)=%r geom=%r" % (c, (m.slots[0]["x"], m.slots[0]["y"])))
        # 三连击：第 3 次按下不得再触发一次 toggle（内核 last_title_wnd=NULL）
        triple(300, 57)
        time.sleep(0.8)
        m.toggle_max(0)
        buf = shot(11)
        c = px(buf, w, 1000, 6)
        rep("C4c: triple-click toggles only once (no double-trigger)",
            title_blue_exact(c), "| (1000,6)=%r" % (c,))
        model_vs_kernel("C4 end")
        cleanup_all()

        # ================= C5 菜单开着点窗口标题栏 =================
        launch("files")                       # slot0 (60,51)
        launch("notepad")                     # slot1 (116,97) 聚焦
        menu_open()
        buf = shot(12)
        c_menu = px(buf, w, 36, 500)
        rep("C5: start menu open (panel gray)", menu_panel_gray(c_menu),
            "| (36,500)=%r" % (c_menu,))
        # 点 files 标题栏 (700,57)：在菜单 rect(x 12..612) 之外 ->
        # 菜单应关，且 press 继续走窗口命中 -> files 获焦
        qmp.click(700, 57)
        time.sleep(1.0)
        m.menu = False
        # 点标题栏 = 该窗置前（内核 gw_bring_front）——模型必须跟上，
        # 否则 C5 end 的模型对账会误报 foc 不一致（2026-10-24 实测）
        m.bring_front(0)
        buf = shot(13)
        dump("C5 after menu+title click", buf)
        c_menu2 = px(buf, w, 36, 500)
        c_fl = px(buf, w, 500, 57)            # files 标题中心
        rep("C5a: clicking window title outside menu rect closes menu",
            not menu_panel_gray(c_menu2), "| (36,500)=%r" % (c_menu2,))
        rep("C5b: same click focuses the clicked window",
            title_blue_exact(c_fl), "| files_t=%r (blue=已获焦)" % (c_fl,))
        model_vs_kernel("C5 end")
        cleanup_all()

        # ================= C6 全最小化后 Alt+Tab =================
        launch("files")                       # slot0
        launch("notepad")                     # slot1（聚焦在 slot1）
        # 任务栏钮语义：对**聚焦**窗点一次=最小化；对**非聚焦**窗点一次
        # 只是恢复置顶。所以每个窗要点两次：先恢复置顶，再最小化。
        for s in (0, 1):
            bx, tw = tb_btn(s)
            qmp.click(bx + tw // 2, TB_Y)
            time.sleep(1.2)
            m.restore(s)
            qmp.click(bx + tw // 2, TB_Y)
            time.sleep(1.2)
            m.minimize(s)
        qmp.hmc("sendkey alt-tab")
        time.sleep(0.6)
        m.alt_tab()
        f.assert_desk("C6: after all-minimized + alt-tab screen is clean")
        model_vs_kernel("C6 end")
        cleanup_all()

        # ================= C7 关非聚焦窗 + slot 复用 =================
        launch("clock")                       # slot0 (60,51,400x512)
        launch("notepad")                     # slot1 (116,97,760x640)
        model_vs_kernel("C7 after 2 launches")
        # 把 clock 拖到右下角，让出 notepad 的关闭钮
        qmp.drag(90, 57, 900, 700)
        time.sleep(0.8)
        m.bring_front(0)
        m.slots[0]["x"] = clamp(60 + (900 - 90), 0, GFX_W - 400)
        m.slots[0]["y"] = clamp(51 + (700 - 57), 0, DESK_BOTTOM - 512)
        rep("C7: clock dragged to free space",
            (m.slots[0]["x"], m.slots[0]["y"]) == (870, 480),
            "| clock=%r" % ((m.slots[0]["x"], m.slots[0]["y"]),))
        model_vs_kernel("C7 after drag")
        cx, cy = title_close_pt(m.slots[1])
        qmp.click(cx, cy)
        time.sleep(1.0)
        m.close(1)
        model_vs_kernel("C7 after close non-focused")
        buf = shot(14)
        dump("C7 after close non-focused", buf)
        c_ck = px(buf, w, 1070, 486)          # clock 标题中心 -> 蓝=仍聚焦
        c_np = px(buf, w, 496, 103)           # notepad 标题中心 -> 已消失
        rep("C7a: closing non-focused window keeps focus",
            title_blue_exact(c_ck), "| clock_t=%r (blue=焦点未转移)" % (c_ck,))
        rep("C7b: closed window leaves no residue",
            not title_blue_exact(c_np) and not is_title_gray(c_np),
            "| notepad_t=%r" % (c_np,))
        # slot 释放后再开窗：取最低空 slot(1)，cascade 按 used=1 重算
        launch("sysinfo")
        buf = shot(15)
        dump("C7 after relaunch", buf)
        c = px(buf, w, 116 + 340, 97 + 6)      # cascade=1 -> (116,97)
        rep("C7c: new window takes lowest free slot with recomputed cascade",
            title_blue_exact(c) and m.find_app(2) == 1,
            "| (456,103)=%r slot=%r" % (c, m.find_app(2)))
        model_vs_kernel("C7 end")
        cleanup_all()

        # ================= C8 长链 60 步（中途不重置）=================
        kinds = []
        for k, wt in [("launch", 22), ("taskbar", 16), ("drag", 12),
                      ("alt_tab", 12), ("close", 10), ("type", 8),
                      ("desktop", 8), ("dbltitle", 6), ("menu_toggle", 4),
                      ("menu_row", 4), ("minbtn", 8)]:
            kinds.extend([k] * wt)
        print("\n----- combo2 long chain seed=%d ops=%d -----" % (seed, CHAIN_OPS), flush=True)
        aborted = False
        for i in range(CHAIN_OPS):
            kind = random.choice(kinds)
            try:
                desc = f.do_op(kind)
            except Exception as e:
                rep("C8 op#%d %s raised" % (i, kind), False, "| %r" % (e,))
                aborted = True
                break
            if not f.check_fast("C8 op#%d %s" % (i, desc)):
                aborted = True
                break
            # 每步都对账（轻量、只在不一致时记断言）：状态漂移类 bug 必须
            # 知道"从哪一步开始分家"，否则只能看到结果对不上、定位不到原因
            chain_check("C8 op#%d" % (i + 1))
            if (i + 1) % 15 == 0:
                # 先对账模型-内核（0 截图，只发 F12），再做像素判定：
                # 像素红了必须先确认"模型与内核一致"才能下结论说内核错
                model_vs_kernel("C8 op#%d" % (i + 1))
                f.assert_focus_is("C8 op#%d" % (i + 1), m.focused)
        if not aborted:
            f.round_end("C8 final")
            rep("C8: gui alive after %d-op long chain" % CHAIN_OPS,
                alive(qmp, S, w, h, 14))
            rep("C8: desktop clean after long chain",
                not m.used(), "| residue=%r" % ([(s, m.slots[s]["app"])
                                                for s in m.used()],))

        # ================= C9 连点标题栏最小化按钮 =================
        # 内核缺陷候选：标题栏最小化按钮被点 -> gw_minimize 隐藏窗口 ->
        # 窗口没了，同一屏幕坐标变成"桌面空白" -> 第二次按下走桌面分支
        # -> "恢复最低 slot 的隐藏窗" -> **刚最小化的窗口被立刻恢复**
        # （连点最小化等于没最小化；点别的窗口同位置还会顺带把那个隐藏窗
        #  拽出来）。判据用**内核转储**（机器判定，不靠像素）。
        launch("files")                       # slot0 (60,51,880x640)
        w0 = m.slots[0]
        qmp.move_to(w0["x"] + w0["w"] + MIN_DX, w0["y"] + TITLE_CY_OFF)
        for _ in range(2):
            qmp.hmc("mouse_button 1")
            time.sleep(0.06)
            qmp.hmc("mouse_button 0")
            time.sleep(0.06)
        time.sleep(1.2)
        hdr9, wins9 = kernel_state()
        hid9 = wins9[0]["hid"] if wins9 else None
        rep("C9: second click on titlebar-min must NOT restore the window",
            hid9 == 1,
            "| kernel hid=%r focus=%r (hid=1=仍最小化)" %
            (hid9, hdr9["focus"] if hdr9 else None))
        cleanup_all()

        # ================= C10 拖动 + 边界钳制 + 关闭命中 =================
        # 组合轴：拖动 -> 边界钳制 -> 在被钳制的新位置上关闭。
        # 内核 gw_handle_mouse 的拖动钳制：nx+w > desktop_right -> nx =
        # desktop_right-w；ny+h > desktop_bottom -> ny = desktop_bottom-h。
        # 钳制后窗口右缘正好贴屏幕右缘（1280），关闭钮落在 [1268,1279)
        # ——必须仍能点中（否则"拖到边上的窗口关不掉"）。
        launch("files")                       # slot0 (60,51,880x640)
        w0 = m.slots[0]
        x0, y0 = w0["x"] + 30, w0["y"] + 6    # 标题栏内一点
        qmp.drag(x0, y0, 1100, 700)
        time.sleep(0.8)
        m.bring_front(0)
        nx = clamp(w0["x"] + (1100 - x0), 0, GFX_W - w0["w"])
        ny = clamp(w0["y"] + (700 - y0), 0, DESK_BOTTOM - w0["h"])
        w0["x"], w0["y"] = nx, ny
        rep("C10: drag to far corner is clamped inside desktop",
            (nx, ny) == (GFX_W - w0["w"], DESK_BOTTOM - w0["h"]),
            "| clamped=%r (期望 %r)" %
            ((nx, ny), (GFX_W - w0["w"], DESK_BOTTOM - w0["h"])))
        model_vs_kernel("C10 after clamped drag")
        # 画面必须真的重绘到钳制后的新位置（标题栏中心 = x+w/2, y+6）
        buf10 = shot(16)
        c10 = px(buf10, w, nx + w0["w"] // 2, ny + TITLE_CY_OFF)
        rep("C10: window is redrawn at the clamped position",
            title_blue_exact(c10),
            "| (%d,%d)=%r (blue=聚焦标题栏在新位置)" %
            (nx + w0["w"] // 2, ny + TITLE_CY_OFF, c10))
        # 钳制后的关闭钮仍必须点得到（窗口贴着屏幕右缘）
        hdr10, wins10 = kernel_state()
        if wins10:
            gw10 = wins10[0]
            cx10 = gw10["x"] + gw10["w"] + CLOSE_DX
            cy10 = gw10["y"] + TITLE_CY_OFF
            qmp.click(cx10, cy10)
            time.sleep(1.2)
            hdr10b, wins10b = kernel_state()
            rep("C10: close button still clickable on a clamped window",
                hdr10b is not None and hdr10b["n"] == 0,
                "| close at (%d,%d) -> n=%r" %
                (cx10, cy10, hdr10b["n"] if hdr10b else None))
        else:
            rep("C10: close button still clickable on a clamped window",
                False, "| no window in kernel dump")
        cleanup_all()

        # ================= C11 最大化态拖动必须被忽略 =================
        # 内核 gw_handle_mouse 拖动分支：maximized 窗口直接 moving=0
        # （几何一个像素都不许动）。这条钉的是"最大化后还能不能拖着走"——
        # 若去掉 max 判据，窗口会被拖成半屏悬空，标题栏按钮也可能跑出
        # 屏幕，判据用**内核转储**（几何前后逐字段比）。
        launch("files")                       # slot0 (60,51)
        qmp.dblclick(300, 57)                 # 双击标题栏 -> 最大化
        time.sleep(0.8)
        m.toggle_max(0)
        model_vs_kernel("C11 after maximize")
        hdr11a, wins11a = kernel_state()
        geo0 = None
        if wins11a:
            g = wins11a[0]
            geo0 = (g["x"], g["y"], g["w"], g["h"], g["max"])
        qmp.drag(300, 6, 700, 400)            # 最大化态拖标题栏
        time.sleep(1.0)
        hdr11b, wins11b = kernel_state()
        geo1 = None
        if wins11b:
            g = wins11b[0]
            geo1 = (g["x"], g["y"], g["w"], g["h"], g["max"])
        rep("C11: maximized window ignores title-bar drag",
            geo0 is not None and geo0 == geo1,
            "| before=%r after=%r" % (geo0, geo1))
        m.bring_front(0)          # 标题栏按下仍会置前（模型要跟上）
        cleanup_all()

        # ================= C12 同一 app 重复启动 =================
        # 内核 gw_launch_app：已有该 app 的窗口 -> restore 置前，**不新建**。
        # 判据：转储里该 app 仍然只有 1 个窗口，且 slot 不变。
        launch("files")                       # slot0
        hdr12a, wins12a = kernel_state()
        slot0_12 = wins12a[0]["slot"] if wins12a else None
        launch("files")                       # 再来一次
        hdr12, wins12 = kernel_state()
        nfiles = len([w for w in wins12 if w["app"] == APP_IDX["files"]])
        rep("C12: re-launching the same app reuses the existing window",
            nfiles == 1 and len(wins12) == 1 and wins12[0]["slot"] == slot0_12,
            "| wins=%r (期望 1 个 files, slot 不变=%r)" %
            ([(w["slot"], w["app"]) for w in wins12], slot0_12))
        model_vs_kernel("C12 after re-launch")
        cleanup_all()

        # ================= C13 菜单盖住标题栏左段 =================
        # 开始菜单面板 x 12..612 会盖住窗口标题栏的左半段。点被盖住的那
        # 一段：内核 gw_start_menu_mouse 返回 0（点在非交互区）-> 调用方
        # 关掉菜单并把事件**继续交给窗口层** -> 该窗获焦。
        # （菜单 rect 之外的那段是 C5 覆盖的路径，两条路径行为必须一致。）
        launch("files")                       # slot0 (60,51)
        launch("notepad")                     # slot1 (116,97) 聚焦
        menu_open()
        qmp.click(520, 57)                    # files 标题栏、菜单覆盖区内
        time.sleep(1.2)
        m.menu = False
        m.bring_front(0)
        hdr13, wins13 = kernel_state()
        foc13 = [w["slot"] for w in wins13 if w["foc"]]
        rep("C13: click on title bar under the menu closes menu and focuses it",
            hdr13 is not None and not hdr13["menu"] and foc13 == [0],
            "| menu=%r focus=%r (期望 menu=0 focus=[0])" %
            (hdr13["menu"] if hdr13 else None, foc13))
        cleanup_all()

        # ================= C14 快速大步进拖动不掉窗 =================
        # 老实现用 ±8/±16 的容差盒去找"拖动中的窗口"，等价于给单包位移设了
        # 上限：向上拖第一包要求 step <= move_dy+8，向下拖要求 step <
        # 20-move_dy。快速拖动时**第一个包**就掉出容差 → hit 落到别的窗口/
        # 桌面 → 拖动态再也找不回来 → 窗口一步不动（2026-10-24 combo2 C8
        # op#70：12px 步进向上拖 384px，模型到了 (0,83)、内核还停在
        # (581,467)）。修法 = 指针抓取：左键没松就由该窗独占拖拽，不做几何
        # 判据。
        # 判据必须能区分两者：**按下点取标题栏底沿（move_dy=2）+ 向上拖 +
        # 单包 24px**。横向不行（x 容差 ±16 > 12）；向下拖第一包判据
        # my < w->y+20 也过得去（dbg_dragv.py 四方向实测）。
        # 只做**一次**拖动：连续两次拖同一窗标题栏会踩 400ms 双击判定——
        # GUI 任务在 TCG 下 CPU 饥饿，事件处理延迟可达数百 ms，两次本无
        # 关联的 press 会在 tick 时间上落进双击窗口 -> 假最大化
        # （dbg_dclick.py 实测 dt=201/399）。
        launch("files")                       # slot0 (60,51) 880x640
        hdr14a, wins14a = kernel_state()
        if not wins14a:
            rep("C14: fast drag (24px per packet) keeps following the cursor",
                False, "| 没有窗口")
        else:
            g = wins14a[0]
            x0, y0 = g["x"] + 150, g["y"] + 2      # move_dy=2（容差最紧）
            want = (g["x"], max(0, g["y"] - 40))
            qmp.drag(x0, y0, x0, y0 - 40, step=24)
            time.sleep(1.0)
            hdr14b, wins14b = kernel_state()
            got = (wins14b[0]["x"], wins14b[0]["y"]) if wins14b else None
            rep("C14: fast drag (24px per packet) keeps following the cursor",
                got == want,
                "| want=%r got=%r (before=%r)" % (want, got, (g["x"], g["y"])))
            m.slots[0]["y"] = want[1]
        cleanup_all()

        # ================= C15 拖动全程无半成品帧（blit 快路径）===========
        # 老实现每帧用"旧位置∪新位置"合并区重绘：局部桌面 + **整窗内容全量
        # 重画** ≈ 1.1M 像素/帧（880x640 窗口，含数千次文字/图标/渐变）。
        # QEMU TCG 下单帧 200-400ms，超过鼠标包间隔（真机 PS/2 100Hz 只有
        # 10ms）——screendump 与肉眼持续看到"桌面已擦、窗口还没画完"的半
        # 成品帧（2026-10-25 用户报"闪动还是太严重"；dbg_flicker.py 实测：
        # 24 帧里 4 成不完整、内容匹配率最低 19%、帧间 diff 峰值 47 万）。
        # 修法 = 显存内 32 位块搬移整个窗口矩形（内容相对窗口不变），只重
        # 画旧位置露出的 |d| 像素窄带。修复后：0/24 不完整、diff 峰值 4.7 万。
        # 判据：逐帧内容匹配率（与拖动前参考帧比，半成品帧会掉到 0.9 以下；
        # 正常帧 0.95+）。帧间 diff 只打印（20 倍差距，不设硬阈值防误报）。
        # 两段之间必须**释放再按**：连续两次按同一窗标题栏会踩 400ms 双击
        # 判定（TCG 下 GUI 任务 CPU 饥饿，事件延迟可达数百 ms，见 C14 注释）。
        launch("files")
        hdr15a, wins15a = kernel_state()
        if not wins15a:
            rep("C15: drag leaves no half-drawn frame", False, "| 没有窗口")
        else:
            g15 = wins15a[0]
            IN_X, IN_Y = g15["x"] + 2, g15["y"] + 16
            IN_W, IN_H = g15["w"] - 4, g15["h"] - 20
            rpath = S[shots[0] % len(S)]
            shots[0] += 1
            qmp.shot(rpath)
            _rw, _rh, rbuf = ppm_load(rpath)

            def flick_round(dx, dy, n, step):
                """一段小步进拖动：返回 (匹配率列表, 粗略 diff 峰值)。
                dx/dy 只能是 (1,0) 或 (0,1)（内核快路径只覆盖纯横/纯竖）。"""
                ratios, peak = [], 0
                prev = None
                hdr_g, wins_g = kernel_state()
                if not wins_g:
                    return [0.0], 0
                gg = wins_g[0]
                sx, sy = gg["x"] + 100, gg["y"] + 2
                qmp.move_to(sx, sy)
                time.sleep(0.2)
                qmp.hmc("mouse_button 1")
                time.sleep(0.15)
                for i in range(n):
                    qmp.hmc("mouse_move %d %d" % (dx * step, dy * step))
                    qmp.cx += dx * step
                    qmp.cy += dy * step
                    time.sleep(0.28)
                    p = S[shots[0] % len(S)]
                    shots[0] += 1
                    qmp.shot(p)
                    w, h, buf = ppm_load(p)
                    wx = gg["x"] + (i + 1) * step * dx
                    wy = gg["y"] + (i + 1) * step * dy
                    same = tot = 0
                    for yy in range(0, IN_H, 3):
                        ro = ((IN_Y + yy) * _rw + IN_X) * 3
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
                time.sleep(0.4)
                return ratios, peak

            ratios = []
            peaks = []
            for (fdx, fdy) in ((0, 1), (1, 0)):
                r, pk = flick_round(fdx, fdy, 6, 6)
                ratios.extend(r)
                peaks.append(pk)
            worst = min(ratios) if ratios else 0.0
            rep("C15: drag leaves no half-drawn frame",
                worst >= 0.90,
                "| frames=%d worst-match=%.2f diff-peaks=%r"
                % (len(ratios), worst, peaks))
            m.slots[0]["x"] += 36
            m.slots[0]["y"] += 36
        cleanup_all()

        # report
        print("\n===== probe_combo2 results (seed=%d) =====" % seed)
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
