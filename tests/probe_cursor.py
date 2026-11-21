# -*- coding: utf-8 -*-
"""probe_cursor.py - "有时候鼠标会消失"的机器判据

背景（2026-10-25 用户反馈）：
  光标"有时候"从画面上消失。两条独立根因，都在 kernel/gfxwin.c：

  1) **重绘前擦光标（主根因）**：主循环老实现把 gw_cursor_restore_bg()
     放在所有重绘**之前**——重绘多久，光标就从帧缓冲里消失多久。点一次
     鼠标触发 gw_draw_all 全屏重绘（壁纸 100 万像素+全部窗口+任务栏），
     TCG 下 100-300ms、真机 5-15ms；Browser 小恐龙每 16ms 一帧全程如此；
     SysInfo 每秒一次。修复：擦除改到所有重绘**之后**（损伤矩形排除
     已被重画成新内容的像素），消失窗口只剩微秒级的 擦+存+画。
  2) **精灵不钳位**：鼠标逻辑坐标能到 (GFX_W-1, GFX_H-1)，16x16 箭头
     15/16 列（行）画在屏幕外——甩到右/下边缘时"鼠标消失了"。修复：
     钳精灵原点，不钳热点。

机器判据（不靠肉眼）：
  A. 光标可见性：screendump 后在**内核报的指针位置**（F12 转贮的
     mx/my，不是探针模型）按 16x16 Win10 箭头精灵的 6 个判据像素
     （黑描边+白体+白尾）精确比对。半成品/不存在都判不出来。
  B. 残影：光标离开某点后，该点必须**不再**呈现精灵图案。
  C. stale 补丁：内容在光标底下发生变化后，旧光标位置的 16x16 必须
     是新内容（损伤排除失效会把旧缓存写回，留下一块永不刷新的旧画面）。
  D. 消失窗口预算：F12 转贮的 cgap=（擦除->重画最大间隔,ms）必须
     <= CGAP_MAX。这一条对根因 1 是确定性的（不依赖 screendump 抓时机）。
  E. 边缘钳位：鼠标在 (GFX_W-2, GFX_H-2) 时精灵必须完整在屏内。

退出码：全部判据过 = 0，任一不过 = 1。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

from probe_combo import (Qmp, ppm_load, px, START_BTN,  # noqa: E402
                         MENU_X, MENU_Y0, MENU_ROW_H, TITLE_CY_OFF)
from probe_fuzz import (Fuzzer, FuzzModel, BOOT_WAIT,  # noqa: E402
                        GFX_W, GFX_H, icon_rects)
from gui_probe import mouse_sync  # noqa: E402
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe, accel_args  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "cur_serial.log").replace("\\", "/")
ERRLOG = os.path.join(ROOT, "temp", "cur_qemu_err.log").replace("\\", "/")
QMP_PORT = alloc_port()
NSHOT = 14          # 截图槽位（沙箱单命令文件操作阈值 ~50，全探针累计 <30）
# 消失窗口上限(ms)：修复后只剩 擦256+存256+画256。**cgap 是访客毫秒，
# 绝对值随加速器缩放**：TCG 下基线 2-3ms、M8 变异 ~11ms；WHPX 下接近
# 原生速度，等比压缩。预算按 stderr 里 "whpx: initialized" 分档，保证
# 两种加速下基线不误红、M8 变异都有牙（实测校准，见 C10 断言）。
CGAP_MAX_TCG = 5
CGAP_MAX_WHPX = 2
SPRITE = 16         # 精灵边长
# 开始菜单行号（与内核 gw_app_names 序一致）：files/notepad/sysinfo/browser
ROW_FILES, ROW_NOTEPAD, ROW_SYSINFO, ROW_BROWSER = 0, 1, 2, 8
ROW_CLOCK = 3

# 16x16 Win10 箭头判据像素（(i,j,期望RGB)，'B'=黑描边 'W'=白体/白尾）。
# 取顶点黑、左列黑、白体、白尾两处——任何单点判据都会被桌面渐变/
# 窗口内容偶然匹配，六点合取误判率可忽略。
SPR = ((0, 0, (0, 0, 0)), (0, 1, (0, 0, 0)), (1, 1, (255, 255, 255)),
       (2, 2, (255, 255, 255)), (8, 11, (255, 255, 255)),
       (8, 15, (255, 255, 255)))

# QEMU sendkey 名字：':' 不是合法 key name（实测 invalid parameter），
# 要用 shift-semicolon；'/' 是 slash。
KEYMAP = {":": "shift-semicolon", "/": "slash", ".": "dot", "-": "minus"}


def rm(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
    except Exception:   # 沙箱 safe-delete 抛的非 OSError 也要吞
        pass


def key_name(ch):
    return KEYMAP.get(ch, ch)


def sprite_origin(mx, my):
    """精灵实际被画到的原点（与内核 gw_cursor_draw/save_bg 同一套钳制）"""
    return (min(mx, GFX_W - SPRITE), min(my, GFX_H - SPRITE))


def sprite_hit(buf, w, ox, oy):
    """精灵图案是否完整出现在 (ox,oy)。返回 (ok, 失败明细)。"""
    bad = []
    for (i, j, want) in SPR:
        x, y = ox + i, oy + j
        if x < 0 or y < 0 or x >= w:
            bad.append("(%d,%d) off-screen" % (x, y))
            continue
        got = px(buf, w, x, y)
        if got != want:
            bad.append("(%d,%d)=%r!=%r" % (i, j, got, want))
    return (not bad), "; ".join(bad)


def region_same(a, b, w, x0, y0, x1, y1):
    """两块缓冲在矩形上是否逐字节相同。返回 (same, diff像素数)。"""
    d = 0
    n = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            o = (y * w + x) * 3
            n += 1
            if a[o:o + 3] != b[o:o + 3]:
                d += 1
    return (d == 0), d


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    rm(LOG)
    rm(ERRLOG)
    errf = open(ERRLOG, "wb")
    proc = subprocess.Popen(
        [QEMU] + accel_args() +
        ["-machine", "pc", "-vga", "std", "-m", "128",
         "-drive", "format=raw,file=" + IMG,
         "-drive", "format=raw,file=" + DISK,
         "-display", "none", "-no-reboot",
         "-serial", "file:" + LOG,
         "-qmp", "tcp:127.0.0.1:%d,server,nowait" % QMP_PORT],
        cwd=ROOT, stdout=errf, stderr=errf)
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
        errf.close()
        # 实际启用的加速器决定 cgap 预算档位（fallback 可能静默落回 tcg）。
        # WHPX 就绪消息打在 stdout：实测 qemu-portable-20241220 输出
        # "Windows Hypervisor Platform accelerator is operational"。
        whpx = (b"Windows Hypervisor Platform accelerator is operational"
                in open(ERRLOG, "rb").read())
        cgap_budget = CGAP_MAX_WHPX if whpx else CGAP_MAX_TCG
        print("   [accel] %s (cgap budget %dms)"
              % ("whpx" if whpx else "tcg", cgap_budget), flush=True)
        qmp = Qmp(QMP_PORT)
        qmp.cx, qmp.cy = 640, 512
        qmp.line("desktop")
        time.sleep(35)

        m = FuzzModel()
        S = [os.path.join(ROOT, "temp", "cur_s%d.ppm" % i).replace("\\", "/")
             for i in range(NSHOT)]
        shots = [0]
        f = Fuzzer(qmp, m, S, shots, log=LOG)
        icons = icon_rects()

        all_ok = True
        cgap_seen = 0

        def dump(tag, want_xy=None):
            """取一次内核转贮；返回 (hdr, wins)。指针与探针模型不符就重试
            （内核处理鼠标移动有帧延迟，转贮可能拍在半路上）。"""
            nonlocal all_ok
            hdr, wins = None, []
            for attempt in range(3):
                hdr, wins = f.gw_state()
                if hdr is None:
                    continue
                syn, detail = mouse_sync(hdr, qmp, tol=4)
                if syn:
                    return hdr, wins
                time.sleep(0.4)
            print("   [gw] mouse sync FAIL: %s" % detail, flush=True)
            f.rep("%s: kernel/probe pointer sync" % tag, False, detail)
            all_ok = False
            return hdr, wins

        def launch_via_menu(row, tag):
            """开始菜单启动第 row 行应用（比双击桌面图标稳：图标可能被
            已开窗口盖住，gw_icon_covered 会直接跳过它，双击落空）。

            行几何（1280x1024，内核 gw_menu_row_h 压缩后实测）：list_y=130、
            row_h=60，row N 行区间 [130+60N, 190+60N)。老公式
            146+row*72+36（按未压缩行高 72）从 row1 起累积偏移——row1 算出
            254 落进 row2（sysinfo），"notepad 没起来"由此而来（2026-11）。
            dy 兜底收窄到 ±16（半行高 30 内）：老 ±36 会滑进相邻行、
            误触 row9-11（Back to Terminal/Restart/Shut Down）。"""
            nonlocal all_ok
            base = 130 + row * 60 + 30
            launched, wins = False, []
            for dy in (0, 16, -16):
                # 开菜单走 f.ensure_menu_open()（以内核转贮 menu=1 对账，
                # 自适应菜单初始开/关）：裸点开始钮有 toggle 陷阱 + PS/2
                # 按钮包丢失，行点击落空桌面 -> windows=0（2026-11
                # mut_drive M8/M11 两轮实拍，探针在此提前退出）。
                f.ensure_menu_open()
                qmp.click(MENU_X, base + dy)
                time.sleep(2.2)
                hdr, wins = dump(tag)
                if any(v.get("app") == row for v in wins):
                    launched = True
                    break
            f.rep("%s: app launched via menu row %d" % (tag, row), launched,
                  "windows=%d" % (len(wins) if wins else 0))
            if not launched:
                all_ok = False

        def shot(i):
            p = S[shots[0] % NSHOT]
            shots[0] += 1
            qmp.shot(p)
            w, h, buf = ppm_load(p)
            return w, h, buf

        def cur_check(tag, note=""):
            """判据 A：光标在指针位置可见。

            screendump 与内核重绘并发，可能拍到撕裂帧（2026-11 cur-7：
            撞上 gw_draw_start_menu 重画，菜单画一半、精灵还没轮到重画）。
            失败先补拍一次再定罪：真缺陷（精灵丢了）补拍也救不回来。"""
            nonlocal all_ok, cgap_seen
            hdr, _w = dump(tag)
            if hdr is None:
                f.rep("%s: cursor visible" % tag, False, "no [GW] dump")
                all_ok = False
                return None
            mx, my = hdr["mx"], hdr["my"]
            cgap_seen = max(cgap_seen, hdr.get("cgap", 0))
            ox, oy = sprite_origin(mx, my)
            okc, det, retried = False, "", ""
            for attempt in range(2):
                w, h, buf = shot(0)
                okc, det = sprite_hit(buf, w, ox, oy)
                if okc:
                    if attempt:
                        retried = " (first shot torn, retry ok)"
                    break
                time.sleep(1.0)   # 让撞上的重绘画完再补拍
            f.rep("%s: cursor visible at (%d,%d)%s%s" % (tag, mx, my, note,
                                                         retried), okc, det)
            if not okc:
                all_ok = False
            return (mx, my)

        def ghost_check(tag, gx, gy, buf, w):
            """判据 B：光标离开后旧点不得残留精灵图案。"""
            nonlocal all_ok
            okc, det = sprite_hit(buf, w, gx, gy)
            f.rep("%s: no cursor ghost at (%d,%d)" % (tag, gx, gy),
                  not okc, det if okc else "")
            if okc:
                all_ok = False

        # ---------- C1 桌面静止 ----------
        qmp.move_to(400, 300)
        time.sleep(0.3)
        cur_check("C1 idle desktop")
        # C1b 桌面图标 hover（图标重绘路径）：icon5 中心
        ix, iy, iw, ih = icons[5]
        qmp.move_to(ix + iw // 2, iy + ih // 2)
        time.sleep(0.3)
        cur_check("C1b icon hover")
        # C1 的残影：光标已离开 (400,300)
        w, h, buf = shot(1)
        ghost_check("C1", 400, 300, buf, w)

        # ---------- C2 桌面点击（点击->全屏重绘的老路） ----------
        qmp.click(700, 600)
        time.sleep(0.3)
        cur_check("C2 after desktop click")

        # ---------- C3 开 Files，光标落在窗口内容区 ----------
        launch_via_menu(ROW_FILES, "C3")
        qmp.move_to(400, 300)
        time.sleep(0.3)
        cur_check("C3 over files window")
        # C3b 残影：移出窗口到桌面
        qmp.move_to(1000, 800)
        time.sleep(0.3)
        w, h, buf = shot(2)
        ghost_check("C3", 400, 300, buf, w)

        # ---------- C12 stale 补丁（损伤排除是否真的生效） ----------
        # Files 初始选中第 0 行。必须点击另一行才有确定的内容变化；老场景
        # 固定点 (300,200)，空目录/行数不足或重复点已选行都会让场景空转。
        # 从内核转储取窗口真实位置，点第 1 行中心，避免再猜窗口坐标。
        hdr, wins = dump("C12 setup")
        fw = next((v for v in wins if v.get("app") == 0), None)
        f.rep("C12: files window geometry available", fw is not None)
        if fw is None:
            all_ok = False
        if fw is not None:
            A = (fw["x"] + 200, fw["y"] + 13 + 18 + 16 + 8)
            B = (1000, 800)
            qmp.move_to(*B)
            time.sleep(0.3)
            w0, h0, s_ref = shot(3)
            qmp.click(*A)
            time.sleep(0.4)
            qmp.move_to(*B)
            time.sleep(0.3)
            w1, h1, s_after = shot(4)
            # 强制全屏重绘（F12 是按键，主循环任意按键 gw_dirty=1）。
            # 必须走 f.gw_state()：裸 hmc sendkey 会让内核 seq 多跳一格，
            # 探针 f12_seq 对账从此错位（2026-11 cur-7 实测）。
            f.gw_state()
            time.sleep(1.2)
            w2, h2, s_fresh = shot(5)
            bx0, by0 = A[0] - 100, A[1] - 20
            bx1, by1 = A[0] + 100, A[1] + 20
            same_ref, d_ref = region_same(s_ref, s_fresh, w0, bx0, by0, bx1, by1)
            same_wb, d_wb = region_same(s_after, s_fresh, w1, bx0, by0, bx1, by1)
            # 前置：点击确实改变了该区域内容（否则本场景空转，不算通过）
            f.rep("C12: click changed content under cursor",
                  not same_ref, "diff=%d (0=内容没变，场景空转)" % d_ref)
            # 判据 C：写回内容 == 全新重绘的内容
            f.rep("C12: no stale patch after cursor left", same_wb,
                  "diff=%d px (损伤排除失效=旧缓存被写回)" % d_wb)
            if same_ref or not same_wb:
                all_ok = False

        # ---------- C4 拖动（blit 快路径 + 斜向合并区两条） ----------
        # 内核标题栏双击判定 = 同一窗 400 tick 内两次按下 -> toggle_max
        # （gfxwin.c gw_handle_desktop_mouse），两次按下之间没有位置/
        # 拖动破序条件——C4a/C4b 连拖同一窗被误判双击 -> max=1（2026-11
        # cur-7 串口实拍）。tick 速率实测仅墙钟 80-160/s，sleep 拉不开
        # 400 tick，照 probe_fuzz op_drag 的规避法：两条拖动用不同窗口。
        launch_via_menu(ROW_CLOCK, "C4")     # 第二窗（后续场景不用它）
        # 水平拖：Files 标题栏 (460,55) -> (700,55)（窗口 60->300）
        qmp.drag(460, 55, 700, 55, step=12)
        time.sleep(0.3)
        cur_check("C4a after horizontal drag")
        # 斜向拖：Clock 标题栏 (200,103) -> (120,223)。Clock 在 cascade1
        # (116,97,400x512)，标题栏左段 x<300 不被拖走后的 Files（x=300）盖住
        qmp.drag(200, 103, 120, 223, step=12)
        time.sleep(0.3)
        cur_check("C4b after diagonal drag")

        # ---------- C5 开始菜单 ----------
        qmp.click(*START_BTN)
        time.sleep(0.8)
        qmp.move_to(300, 400)
        time.sleep(0.3)
        cur_check("C5 over start menu")
        qmp.key("esc")
        time.sleep(0.5)

        # ---------- C6 任务栏 ----------
        qmp.move_to(900, 1008)
        time.sleep(0.3)
        cur_check("C6 over taskbar")

        # ---------- C7 打字（Notepad） ----------
        launch_via_menu(ROW_NOTEPAD, "C7")
        qmp.move_to(400, 300)
        time.sleep(0.2)
        qmp.keys("hello")
        time.sleep(0.3)
        cur_check("C7 after typing")

        # ---------- C8 SysInfo 每秒重绘 ----------
        launch_via_menu(ROW_SYSINFO, "C8")
        qmp.move_to(400, 400)
        time.sleep(1.6)          # 跨过一个 sec tick（SysInfo 局部重绘）
        cur_check("C8 after sysinfo tick")
        # 关掉 SysInfo：它的每秒 tick 走 gw_redraw_region（z 序感知，相交
        # 窗口整块 gw_draw_window）——C11 的白区残影会在 1 秒内被"自愈"抹
        # 掉，变异 M11 实测因此空转无牙。关窗后 tick 无目标，残影才能存活
        # 到截图。点聚焦窗（刚启动，最前）的关闭钮：[x+w-12, x+w-1) -> x+w-7。
        hdr, wins = dump("C8 close")
        sw = next((v for v in wins if v.get("app") == ROW_SYSINFO), None)
        f.rep("C8: sysinfo geometry available", sw is not None)
        if sw is not None:
            qmp.click(sw["x"] + sw["w"] - 7, sw["y"] + TITLE_CY_OFF)
            time.sleep(0.8)

        # ---------- C9 屏幕边缘（判据 E） ----------
        qmp.move_to(GFX_W - 2, GFX_H - 2)
        time.sleep(0.4)
        hdr, _w = dump("C9")
        if hdr is None:
            f.rep("C9: cursor fully visible at screen edge", False,
                  "no [GW] dump")
            all_ok = False
        else:
            mx, my = hdr["mx"], hdr["my"]
            cgap_seen = max(cgap_seen, hdr.get("cgap", 0))
            w, h, buf = shot(6)
            ox, oy = sprite_origin(mx, my)
            okc, det = sprite_hit(buf, w, ox, oy)
            f.rep("C9: cursor fully visible at edge (%d,%d)" % (mx, my),
                  okc, det or ("sprite origin=(%d,%d)" % (ox, oy)))
            if not okc:
                all_ok = False

        # ---------- C10 消失窗口预算（判据 D） ----------
        f.rep("C10: cursor vanish window cgap<=%dms" % cgap_budget,
              cgap_seen <= cgap_budget, "cgap_max=%dms" % cgap_seen)
        if cgap_seen > cgap_budget:
            all_ok = False

        # ---------- C11 Browser 小恐龙 60FPS 帧泵 ----------
        # 用开始菜单启动（桌面图标此时多被窗口盖住，gw_icon_covered 会跳过）。
        # 地址栏在窗口内容区顶部，点左端聚焦后输入 ezos://dino（内置页，
        # 不走网络），回车，空格开始。
        launch_via_menu(ROW_BROWSER, "C11")
        qmp.click(270, 208)            # 地址栏左端（内容区 x0=229, 栏内 27 起）
        time.sleep(0.4)
        for ch in "ezos:/" + "/dino":
            qmp.hmc("sendkey " + key_name(ch))
            time.sleep(0.12)
        qmp.hmc("sendkey ret")
        time.sleep(1.5)
        qmp.hmc("sendkey spc")         # 开始跑
        time.sleep(1.0)
        # 前置：小恐龙真的在跑。采样整个页面区（含分数、云和地面），
        # 不能只采中部：页面大段纯白，老固定框只碰到地面，实测仅 18 个
        # 变化样本而误判成"没运行"。窗口矩形取自内核转储，不猜 cascade。
        qmp.move_to(700, 600)
        time.sleep(0.3)
        hdr, wins = dump("C11 geometry")
        bw = next((v for v in wins if v.get("app") == 8), None)
        f.rep("C11: browser geometry available", bw is not None)
        if bw is None:
            all_ok = False
        if bw is not None:
            wa, ha, sa = shot(7)
            time.sleep(0.7)
            wb, hb, sb = shot(8)
            d = 0
            px0, px1 = bw["x"] + 1, bw["x"] + bw["w"] - 1
            py0, py1 = bw["y"] + 13 + 24, bw["y"] + bw["h"] - 1
            for y in range(py0, py1, 4):
                for x in range(px0, px1, 4):
                    o = (y * wa + x) * 3
                    if sa[o:o + 3] != sb[o:o + 3]:
                        d += 1
            # 静态页两帧应逐点一致，因此 d>0 就足以证明泵在跑；不能设
            # 任意高阈值：TCG 下 0.7s 可能只推进 1 分，4px 抽样实测 18 点。
            f.rep("C11: dino pump running", d > 0, "changed samples=%d" % d)
            if d <= 0:
                all_ok = False
            # 判据 B：泵只回传顶/底两条带，中间白区没重绘。光标从白区移走后
            # 必须靠缓存擦净；若损伤只记两带的总外接框，这里会留下完整残影。
            # 白区点由窗口几何推出：写死屏幕坐标在 cascade 布局下会落到窗口
            # 外（M11 变异曾因此空转无牙）。白区=顶带之下、底带之上的页面区。
            wx = bw["x"] + bw["w"] // 2
            wy = py0 + (py1 - py0) // 4
            qmp.move_to(wx, wy)
            time.sleep(0.5)              # 停 ≥2 个泵帧，旧光标区进入损伤判定
            qmp.move_to(900, 600)
            time.sleep(0.3)
            w, h, buf = shot(9)
            ghost_check("C11", wx, wy, buf, w)
            # 判据 A：光标在页面中部（静态白区）可见
            cur_check("C11 during dino pump")
        # 顺手把 cgap 再读一次（泵帧若还在跑，gap 会在这里体现）
        hdr, _w = dump("C11b")
        if hdr is not None:
            cgap_seen = max(cgap_seen, hdr.get("cgap", 0))
            f.rep("C10b: cgap after dino pump <=%dms" % cgap_budget,
                  hdr.get("cgap", 0) <= cgap_budget,
                  "cgap=%dms" % hdr.get("cgap", 0))
            if hdr.get("cgap", 0) > cgap_budget:
                all_ok = False

        print("\n===== probe_cursor: %s (cgap_max=%dms budget=%dms) =====" %
              ("ALL PASS" if all_ok else "HAS FAILURES", cgap_seen,
               cgap_budget))
        return 0 if all_ok else 1
    finally:
        try:
            errf.close()
        except Exception:
            pass
        try:
            qmp.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
