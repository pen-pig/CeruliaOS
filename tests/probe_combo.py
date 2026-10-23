# -*- coding: utf-8 -*-
"""probe_combo.py - GUI 组合操作压力回归（单操作不炸、组合才炸的 bug class）

probe_browser/probe_gui/test_wm 全是**单窗口串行**：每步只开一个窗、操作后
关窗再下一步，从不验证状态叠加。真实用户开着 3-4 个窗口混合点击/打字/
拖动/菜单，操作间有状态累积、焦点残留、z 序切换、slot 回收。本探针专测
这些组合路径：

  A. 多窗口焦点路由 + 失焦：Browser 地址栏打字 -> 开 Notepad 打字 ->
     点 Browser 露出区切回 -> 再打字。正确行为：第二次打字不进地址栏
     （点页面区=失焦）。测 wv_bar_focus 失焦残留。
  B. 菜单开着点窗口（菜单 rect 外的窗口区域）：菜单应关且窗口事件不被吞，
     后续关闭钮仍可用。
  C. 重叠窗口点击下层露出区：bring_front 置顶切换（z 序组合）。
  D. 开关窗口压力 4 轮：每轮真关窗，spawn 位置必须回到 cascade 0
     （slot 回收，无 used 泄漏）。
  E. 混合事件流：拖动窗口 -> 新位置地址栏打字 -> 菜单开 Settings ->
     主题切换 -> 切回（拖动后 hit 域跟随 + 菜单 + 主题叠加）。
  F. 任务栏多按钮：Files+Notepad 双任务栏钮，最小化/恢复 + 存活。

窗口模型（探针侧模拟内核，全部经 [MS] 串口诊断核对）：
  cascade = spawn 时 used 窗口数；位置 (60+56n, 51+44n)，n=cascade%4
  gw_create 钳制：x+ww>1276 -> x=1276-ww；y+wh>988 -> y=988-wh
  gw_launch_app 去重：app 已开则 gw_restore（hidden=0 + bring_front），不 spawn
  slot = 第一个空位；gw_close 只清 used；关窗后 cascade 回收
  露出区：被完全包含的窗口（如 Browser 1000x768 内的 Notepad 760x640）
  只能靠"关掉上层后露出"或任务栏——场景设计保证 close_all 时最后交互的
  窗口就是最后 spawn 的（focused=最上），纯逆序点关闭钮即可。
几何锚点（Win10 主题 1280x1024，gw_scale_x(v)=2v, gw_scale_y(v)≈2.133v）：
  任务栏 y=992..1024(h=32)  开始按钮 x<48 → 点 (24,1008)
  开始菜单 rect (12,15)-(612,998)，列表 list_y=130，row_h=61（12 行压缩后），
  列表 x=12..548 → 行中心 (248, 130+row*61+30)
  Browser ww=1000 wh=768 内容区 (x+1,y+13)；地址栏内容区偏移 x+27..x+952, y+3..y+23
  Files   ww=880  wh=640   Notepad ww=760 wh=640   Settings ww=680 wh=640
  标题栏关闭钮中心 x = wx+ww-7（命中区 wx+ww-12..wx+ww-4）
用法: python tests/probe_combo.py
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "combo_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()
BOOT_WAIT = 90.0

# app -> (ww, wh, taskbar title)
APP_WH = {
    "files":    (880, 640, "File Manager"),
    "notepad":  (760, 640, "Notepad"),
    "settings": (680, 640, "Settings"),
    "browser":  (1000, 768, "Browser"),
    "about":    (640, 384, "About Cerulia"),
    "terminal": (840, 640, "Terminal"),
    "sysinfo":  (680, 555, "System Info"),
    "clock":    (400, 512, "Clock"),
}
TITLE_CY_OFF = 6
START_BTN = (24, 1008)
MENU_X = 248
MENU_Y0 = 130
MENU_ROW_H = 61
# Browser cascade0 (60,51)：地址栏绝对区域（内容区 61,64 + 栏偏移 27..952, 2..22）
BAR_X0, BAR_Y0, BAR_X1, BAR_Y1 = 90, 66, 1010, 86
# Notepad cascade1 (116,95) 内容区 (117,108)：文本区
NP_X0, NP_Y0 = 119, 123


class Wins(object):
    """探针侧窗口模型：按打开顺序记录 (app, x, y, ww, wh)。"""

    def __init__(self):
        self.list = []

    def spawn(self, app):
        cascade = len(self.list)
        ww, wh, _ = APP_WH[app]
        x = 60 + (cascade % 4) * 56
        y = 51 + (cascade % 4) * 44
        if x + ww > 1276:
            x = 1276 - ww
        if y + wh > 988:
            y = 988 - wh
        self.list.append((app, x, y, ww, wh))
        return self.list[-1]

    def launch(self, app):
        """模拟 gw_launch_app：已开则 restore（位置不变、视为 focused）。"""
        for i, w in enumerate(self.list):
            if w[0] == app:
                # restore 只改 focused 不改位置——但场景设计保证 launch 已开
                # app 时不依赖位置变化，直接返回 None 表示无新窗口
                return None
        return self.spawn(app)

    def close_all(self, qmp, S, w, h):
        """逐窗关闭（标题栏像素 + 帧差双验证）：对每个窗口点其关闭钮，截图
        对比——窗口标题栏中心由标题蓝变非蓝 = 该窗口确实被关掉（pop）；
        仅光标/时钟的微小变化 = 点击落空（窗口被上层盖住），换下一个。
        每轮从后往前扫描，直到全部关净。不能纯逆序：focused 的窗口才在
        最上，而它不一定是最后 spawn 的（A 场景 Browser 先开但最后交互），
        被完全包含的窗口（Notepad 在 Browser 1000x768 内）关闭钮永远点不
        中，必须等盖住它的上层关掉后露出。

        为什么不能只用帧差：关一个被大部分盖住的窗口只改变 2.5%~5.9%
        的画面（Notepad 底缘露出条 / Files-Notepad 差集），低于任何合理
        阈值——2026-10-23 实测 C 场景两窗都关成功了却因 diff 不达标
        全部没 pop，模型残留污染 D/E。标题栏中心像素不受覆盖率影响
        （cascade 偏移 56/44 保证各窗标题中心不重合），是主判据；
        帧差作兜底（防"关掉的窗口位置上恰好是另一个窗口的标题栏"）。"""
        guard = 0
        while self.list and guard < 24:
            guard += 1
            progress = False
            for i in range(len(self.list) - 1, -1, -1):
                app, x, y, ww, wh = self.list[i]
                tcx, tcy = x + ww // 2, y + TITLE_CY_OFF
                qmp.shot(S[0])
                _, _, d0 = ppm_load(S[0])
                blue_before = is_title_blue(px(d0, w, tcx, tcy))
                qmp.click(x + ww - 7, y + TITLE_CY_OFF)
                time.sleep(1.0)
                qmp.shot(S[1])
                _, _, d1 = ppm_load(S[1])
                dd, nn = frame_diff(d0, d1, w, h, step=8)
                blue_after = is_title_blue(px(d1, w, tcx, tcy))
                # 关成功 = 标题栏中心从蓝变非蓝；帧差作兜底
                if (blue_before and not blue_after) or dd * 10 > nn:
                    self.list.pop(i)
                    progress = True
                    break
            if not progress:
                return False
        return not self.list


def rm(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


class Qmp(object):
    def __init__(self, port):
        import json
        import socket
        self.json = json
        for _ in range(80):
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), 2)
                break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("QMP connect failed")
        self.sock.settimeout(30)
        self.f = self.sock.makefile("rwb")
        self.f.readline()
        self.cmd("qmp_capabilities")
        self.cx, self.cy = 320, 240

    def cmd(self, name, **args):
        self.f.write((self.json.dumps({"execute": name, "arguments": args})
                      + "\n").encode())
        self.f.flush()
        while True:
            line = self.f.readline()
            if not line:
                return {}
            try:
                o = self.json.loads(line)
            except ValueError:
                continue
            if "return" in o or "error" in o:
                return o

    def hmc(self, c):
        return self.cmd("human-monitor-command", **{"command-line": c})

    def line(self, text):
        for ch in text:
            self.hmc("sendkey " + ("spc" if ch == " " else ch))
            time.sleep(0.03)
        self.hmc("sendkey ret")
        time.sleep(3.0)

    def key(self, k):
        self.hmc("sendkey " + k)
        time.sleep(0.2)

    def keys(self, s):
        for ch in s:
            self.key(ch)

    def move_to(self, x, y):
        while self.cx != x or self.cy != y:
            dx = max(-12, min(12, x - self.cx))
            dy = max(-12, min(12, y - self.cy))
            self.hmc("mouse_move %d %d" % (dx, dy))
            self.cx += dx
            self.cy += dy
            time.sleep(0.04)
        time.sleep(0.12)

    def click(self, x, y):
        self.move_to(x, y)
        self.hmc("mouse_button 1")
        time.sleep(0.1)
        self.hmc("mouse_button 0")
        time.sleep(0.25)

    def dblclick(self, x, y):
        self.move_to(x, y)
        self.hmc("mouse_button 1")
        time.sleep(0.06)
        self.hmc("mouse_button 0")
        time.sleep(0.06)
        self.hmc("mouse_button 1")
        time.sleep(0.06)
        self.hmc("mouse_button 0")
        time.sleep(0.4)

    def drag(self, x0, y0, x1, y1):
        """按住拖动：标题栏按下 -> 分步移动 -> 释放在终点"""
        self.move_to(x0, y0)
        self.hmc("mouse_button 1")
        time.sleep(0.12)
        while self.cx != x1 or self.cy != y1:
            dx = max(-12, min(12, x1 - self.cx))
            dy = max(-12, min(12, y1 - self.cy))
            self.hmc("mouse_move %d %d" % (dx, dy))
            self.cx += dx
            self.cy += dy
            time.sleep(0.05)
        time.sleep(0.15)
        self.hmc("mouse_button 0")
        time.sleep(0.3)

    def shot(self, path):
        # 不预删：screendump 直接覆盖写。沙箱对单 turn 累计删除数有阈值
        # （~50），预删会把 finally 的清理额度耗尽触发拦截。
        self.hmc("screendump " + path)
        time.sleep(0.4)
        return path


def ppm_load(fn):
    with open(fn, "rb") as f:
        data = f.read()
    parts = data.split(b"\n", 3)
    w, h = map(int, parts[1].split())
    return w, h, parts[3]


def px(buf, w, x, y):
    o = (y * w + x) * 3
    return (buf[o], buf[o + 1], buf[o + 2])


def is_title_blue(c):
    """Win10 标题栏 (0,120,215)/(0,109,198)。桌面渐变深蓝 g 分量低不误判。"""
    return c[2] > 140 and c[1] >= 90 and c[0] < 90 and c[2] > c[0] + 60


def is_page_white(c):
    """窗口内容区页面底色 #F3F3F3=(243,243,243)：判"窗口存在且在该位置"。
    桌面渐变深蓝 min 分量极低，不会误判。"""
    return min(c) > 200


def frame_diff(a, b, w, h, step=1):
    n = d = 0
    for y in range(0, h, step):
        base = y * w
        for x in range(0, w, step):
            o = (base + x) * 3
            n += 1
            if a[o:o + 3] != b[o:o + 3]:
                d += 1
    return d, n


def bar_dark(buf, w):
    """Browser cascade0 地址栏区域暗像素（黑字）数"""
    n = 0
    for y in range(BAR_Y0, BAR_Y1):
        for x in range(BAR_X0, BAR_X1):
            o = (y * w + x) * 3
            if buf[o] + buf[o + 1] + buf[o + 2] < 200:
                n += 1
    return n


def zone_dark(buf, w, x0, y0, x1, y1):
    n = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            o = (y * w + x) * 3
            if buf[o] + buf[o + 1] + buf[o + 2] < 200:
                n += 1
    return n


def menu_open(qmp):
    qmp.click(*START_BTN)
    time.sleep(0.6)


def menu_item(qmp, row):
    qmp.click(MENU_X, MENU_Y0 + row * MENU_ROW_H + MENU_ROW_H // 2)
    time.sleep(2.5)


def alive(qmp, S, w, h, i):
    """鼠标移动两截帧差：证明主循环仍在跑（非冻结）。
    光标只占屏幕 ~0.2%，step=8 下变化采样点可能低至 1-2 个（光标恰好
    落在采样网格缝里）——阈值取 >=2：冻结时两次截图无任何变化（连秒
    跳变都不发生）d=0，误报窗口极窄。"""
    qmp.shot(S[i])
    _, _, d1 = ppm_load(S[i])
    qmp.move_to(640, 500)
    time.sleep(0.3)
    qmp.move_to(200, 200)
    time.sleep(0.3)
    qmp.shot(S[i + 1])
    _, _, d2 = ppm_load(S[i + 1])
    d, n = frame_diff(d1, d2, w, h, step=8)
    return d >= 2


def main():
    results = []
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
    S = [os.path.join(ROOT, "temp", "combo_s%d.ppm" % i).replace("\\", "/")
         for i in range(24)]
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
        results.append(("boot reaches shell", ok))
        if not ok:
            return 1
        qmp = Qmp(QMP_PORT)
        qmp.cx, qmp.cy = 640, 512
        qmp.line("desktop")
        time.sleep(35)
        qmp.shot(S[0])
        w, h, d0 = ppm_load(S[0])
        results.append(("desktop entered", (w, h) == (1280, 1024)))
        if (w, h) != (1280, 1024):
            return 1
        qmp.cx, qmp.cy = w // 2, h // 2

        # ============ 场景 A：多窗口焦点路由 + 失焦 ============
        # 0 窗起点。Browser 先开（cascade0 60,51），Notepad 后开（cascade1
        # 116,95，被 Browser 1000x768 完全包含——它的关闭只能等 Browser 关掉）
        qmp.key("b")                      # 'b' 键启动 Browser
        wins.spawn("browser")
        time.sleep(2.5)
        qmp.shot(S[1])
        _, _, da1 = ppm_load(S[1])
        results.append(("A: browser launched",
                        is_page_white(px(da1, w, 200, 200)),
                        "| page=%r" % (px(da1, w, 200, 200),)))
        # 点地址栏左端 + 打 aaa
        qmp.click(92, 76)
        time.sleep(0.3)
        qmp.keys("aaa")
        time.sleep(0.5)
        qmp.shot(S[2])
        _, _, da2 = ppm_load(S[2])
        n1 = bar_dark(da2, w)
        results.append(("A: typing into address bar shows chars",
                        n1 > 3, "| bar_dark=%d" % n1))
        # 菜单开 Notepad（cascade1 116,95，launch 后 focused=置顶）
        menu_open(qmp)
        menu_item(qmp, 1)
        wins.spawn("notepad")
        qmp.shot(S[3])
        _, _, da3 = ppm_load(S[3])
        results.append(("A: notepad launched (cascade1)",
                        is_page_white(px(da3, w, 300, 150)),
                        "| page=%r" % (px(da3, w, 300, 150),)))
        # 点 Notepad 内容区聚焦 + 打 zzz
        qmp.click(NP_X0 + 200, NP_Y0 + 40)
        time.sleep(0.3)
        qmp.shot(S[4])
        _, _, da4 = ppm_load(S[4])
        np0 = zone_dark(da4, w, NP_X0, NP_Y0, NP_X0 + 300, NP_Y0 + 45)
        qmp.keys("zzz")
        time.sleep(0.5)
        qmp.shot(S[5])
        _, _, da5 = ppm_load(S[5])
        np1 = zone_dark(da5, w, NP_X0, NP_Y0, NP_X0 + 300, NP_Y0 + 45)
        results.append(("A: keys routed to notepad while it has focus",
                        np1 > np0 + 8,
                        "| notepad dark %d -> %d" % (np0, np1)))
        # 点 Browser 露出区（Notepad 右缘 876 之外）切回焦点。
        # 正确行为：点页面区=地址栏失焦（bring_front 里 wv_bar_set_focus(0)），
        # bbb 应走页面滚动而非插字符
        qmp.click(900, 300)
        time.sleep(0.3)
        qmp.keys("bbb")
        time.sleep(0.5)
        qmp.shot(S[6])
        _, _, da6 = ppm_load(S[6])
        n2 = bar_dark(da6, w)
        results.append(("A: page-area click clears address bar focus",
                        n2 <= n1 + 2,
                        "| bar_dark %d -> %d (expect ~equal)" % (n1, n2)))
        results.append(("A: gui alive after multi-window routing",
                        alive(qmp, S, w, h, 7)))
        # A 收尾：Browser 是最后交互的（focused=最上）→ 纯逆序关窗
        wins.close_all(qmp, S, w, h)
        qmp.shot(S[9])
        _, _, da9 = ppm_load(S[9])
        results.append(("A: cleanup leaves no window",
                        not is_page_white(px(da9, w, 200, 200)),
                        "| desk=%r" % (px(da9, w, 200, 200),)))

        # ============ 场景 B：菜单开着点窗口（菜单 rect 外） ============
        # 0 窗起点（A 已清理）。单 Browser，无重叠——关闭钮永远可点。
        qmp.key("b")
        wins.spawn("browser")
        time.sleep(2.5)
        menu_open(qmp)                    # 菜单开
        qmp.shot(S[9])
        _, _, db1 = ppm_load(S[9])
        # 点 Browser 标题栏 x=800（菜单 rect 只到 612，在菜单外）
        qmp.click(800, 57)
        time.sleep(0.6)
        qmp.shot(S[10])
        _, _, db2 = ppm_load(S[10])
        d_menu, n_menu = frame_diff(db1, db2, w, h, step=4)
        results.append(("B: clicking window (outside menu rect) closes menu",
                        d_menu * 10 > n_menu,
                        "| diff %d/%d" % (d_menu, n_menu)))
        # 菜单关了且窗口事件没被吞：关闭钮应仍能关窗
        qmp.click(60 + 1000 - 7, 57)
        time.sleep(1.2)
        wins.list = []          # 模型同步：Browser 已手动关掉（否则 C 的
                                # cascade 全部偏移一个，close_all 点空——
                                # 2026-10-23 实测：模型残留 [browser] 导致
                                # Files/Notepad 双双泄漏进 D/E）
        qmp.shot(S[11])
        _, _, db3 = ppm_load(S[11])
        results.append(("B: window click not swallowed (close still works)",
                        not is_page_white(px(db3, w, 200, 200)),
                        "| page=%r" % (px(db3, w, 200, 200),)))
        results.append(("B: gui alive", alive(qmp, S, w, h, 12)))
        wins.close_all(qmp, S, w, h)

        # ============ 场景 C：重叠窗口点下层露出区 -> bring_front ============
        # 0 窗起点。Files cascade0 (60,51,880x640) + Notepad cascade1
        # (116,95,760x640)——Notepad 底缘 y∈[691,735] 露出（Files 只到 691）
        qmp.dblclick(69, 74)              # 图标0 = File Manager（cascade0）
        wins.spawn("files")
        time.sleep(2.5)
        menu_open(qmp)
        menu_item(qmp, 1)                 # Notepad（cascade1，launch 后置顶）
        wins.spawn("notepad")
        qmp.shot(S[14])
        _, _, dc1 = ppm_load(S[14])
        p_before = px(dc1, w, 496, 101)   # Notepad 标题栏中心
        results.append(("C: notepad overlaps files (title blue on top)",
                        is_title_blue(p_before),
                        "| before=%r" % (p_before,)))
        # 点 Files 右缘露出区（Notepad 右缘 876 之外）
        qmp.click(905, 300)
        time.sleep(0.8)
        qmp.shot(S[15])
        _, _, dc2 = ppm_load(S[15])
        d_z, n_z = frame_diff(dc1, dc2, w, h, step=4)
        # bring_front 生效 = 聚焦窗口后画 = Notepad 整窗 (760x640=36.8% 屏)
        # 被 Files 盖住 -> diff≈37%；未生效时仅光标/时钟的局部变化 (<5%)
        # 注意：不能靠 (496,101) 颜色判据——Files 置顶后该处是 Files 内容区
        # 工具栏/选中行，可能与 Notepad 标题栏蓝 (0,109,198) 同色。
        results.append(("C: clicking lower window's exposed edge brings it front",
                        is_title_blue(p_before) and d_z * 10 > n_z * 3,
                        "| diff %d/%d" % (d_z, n_z)))
        # C 收尾：点 Notepad 露出底缘 (496,727) 让它 focused=最上，
        # 之后纯逆序关窗（先 Notepad 后 Files）
        qmp.click(496, 727)
        time.sleep(0.5)
        results.append(("C: gui alive", alive(qmp, S, w, h, 16)))
        # ---- C2：拖动非焦点窗口的露出标题栏 -> 拖动即置顶 ----
        # 状态：Notepad(116,95) 在上，Files(60,51) 在下。Files 标题栏露出
        # 条 x∈[60,116)。抓 (80,57)（偏移 20,6）拖到 (100,400)：
        # Files -> (80,394) 被 gw_create 钳制成 (80, 988-640=348)。
        # 置顶后标题栏中心 (520,354) 是 Files 标题蓝；未置顶则被 Notepad
        # 盖住显示其内容（白/文字）——gw_draw_all 对 moving 只画 ghost，
        # 不提升 z 序，故释放后仍按 focused 后画判决。
        qmp.drag(80, 57, 100, 400)
        wins.list[0] = ("files", 80, 348, 880, 640)   # 模型同步拖动结果
        time.sleep(0.5)
        qmp.shot(S[19])
        _, _, dc3 = ppm_load(S[19])
        p_drag = px(dc3, w, 520, 354)
        results.append(("C2: dragging unfocused window's title brings it front",
                        is_title_blue(p_drag),
                        "| title=%r (expect blue (0,120,215))" % (p_drag,)))
        wins.close_all(qmp, S, w, h)

        # ============ 场景 D：开关窗口压力（slot 回收） ============
        # 0 窗起点。每轮真关窗：menu_item(1) -> Notepad cascade0 (60,51)
        # -> close (813,57)。slot 泄漏 -> cascade 递增 -> 窗口偏移 -> 大面积 diff
        d_ok = True
        d_detail = []
        d_shots = []
        for rnd in range(4):
            menu_open(qmp)
            menu_item(qmp, 1)             # Notepad（0 窗 -> cascade0）
            wins.spawn("notepad")
            qmp.shot(S[18])
            _, _, dd = ppm_load(S[18])
            d_shots.append(dd)
            qmp.click(60 + 760 - 7, 57)   # 真关窗
            time.sleep(1.2)
            wins.list = []
        for rnd in (1, 2, 3):
            d_r, n_r = frame_diff(d_shots[0], d_shots[rnd], w, h, step=4)
            d_detail.append("r%d=%d/%d" % (rnd, d_r, n_r))
            if not (d_r * 20 < n_r):
                d_ok = False
        results.append(("D: 4x open/close recycles slot (spawn stays cascade0)",
                        d_ok, "| " + " ".join(d_detail)))
        results.append(("D: gui alive after open/close pressure",
                        alive(qmp, S, w, h, 20)))

        # ============ 场景 E：混合事件流（拖动 -> 打字 -> 菜单 -> 主题） ============
        # 0 窗起点。Browser cascade0 (60,51)，拖到 (260,224)：
        # 抓 (300,57) 偏移 (240,6)，释放 (500,350) -> (260,344) 被底部钳制
        # 到 (260, 992-768=224)
        qmp.key("b")
        wins.spawn("browser")
        time.sleep(2.5)
        qmp.shot(S[21])
        _, _, de0 = ppm_load(S[21])
        qmp.drag(300, 57, 500, 350)
        # 模型更新拖动后位置
        wins.list[-1] = ("browser", 260, 224, 1000, 768)
        qmp.shot(S[22])
        _, _, de1 = ppm_load(S[22])
        d_mv, n_mv = frame_diff(de0, de1, w, h, step=4)
        # 位移 (200,173)：左侧竖带 200x1024 + 底部横带 + 右上角 ≈32%
        results.append(("E: drag moves window to new position",
                        d_mv * 10 > n_mv * 3,
                        "| diff %d/%d" % (d_mv, n_mv)))
        results.append(("E: old position uncovered (not page white)",
                        not is_page_white(px(de1, w, 300, 80)),
                        "| old page=%r" % (px(de1, w, 300, 80),)))
        # 新位置地址栏：内容区 (261,237)，栏左端 x=261+27+2=290, y=237+12=249
        qmp.click(290, 249)
        time.sleep(0.3)
        qmp.keys("hi")
        time.sleep(0.5)
        qmp.shot(S[23])
        _, _, de2 = ppm_load(S[23])
        eb = 0
        for y in range(239, 259):
            for x in range(292, 500):
                o = (y * w + x) * 3
                if de2[o] + de2[o + 1] + de2[o + 2] < 200:
                    eb += 1
        results.append(("E: typing works at dragged position",
                        eb > 3, "| bar_dark=%d" % eb))
        # 菜单开 Settings（cascade1 116,95,680x640）+ 主题行点击
        menu_open(qmp)
        menu_item(qmp, 4)
        wins.spawn("settings")
        qmp.shot(S[3])                    # 复用 S[3]（此前内容已用完）
        _, _, de3 = ppm_load(S[3])
        qmp.click(317, 165)               # 主题 row1（GNOME）
        time.sleep(2.0)
        qmp.shot(S[4])
        _, _, de4 = ppm_load(S[4])
        d_th, n_th = frame_diff(de3, de4, w, h, step=4)
        results.append(("E: theme click switches layout (Win10->GNOME)",
                        d_th * 10 > n_th * 3, "| diff %d/%d" % (d_th, n_th)))
        qmp.click(317, 147)               # 主题 row0 切回 Win10
        time.sleep(2.0)
        qmp.shot(S[5])
        _, _, de5 = ppm_load(S[5])
        d_back, n_back = frame_diff(de4, de5, w, h, step=4)
        results.append(("E: theme click switches back (GNOME->Win10)",
                        d_back * 10 > n_back * 3, "| diff %d/%d" % (d_back, n_back)))
        results.append(("E: gui alive after mixed event stream",
                        alive(qmp, S, w, h, 7)))
        # E 收尾：Settings 是最后交互的（theme 点击）→ 纯逆序关窗
        wins.close_all(qmp, S, w, h)

        # ============ 场景 F：任务栏多按钮 + 最小化/恢复 ============
        # 0 窗起点。Files cascade0 + Notepad cascade1
        qmp.dblclick(69, 74)              # Files
        wins.spawn("files")
        time.sleep(2.5)
        menu_open(qmp)
        menu_item(qmp, 1)                 # Notepad
        wins.spawn("notepad")
        time.sleep(1.0)
        # 先点 Files 露出区让它 focused（否则任务栏钮点击=恢复置顶而非最小化）
        qmp.click(905, 300)
        time.sleep(0.8)
        qmp.shot(S[9])
        _, _, df1 = ppm_load(S[9])
        qmp.click(84, 1008)               # Files 任务栏钮（focused -> 最小化）
        time.sleep(1.5)
        qmp.shot(S[10])
        _, _, df2 = ppm_load(S[10])
        d_min, n_min = frame_diff(df1, df2, w, h, step=4)
        results.append(("F: taskbar button minimizes Files (multi-window)",
                        d_min * 5 > n_min,
                        "| diff %d/%d" % (d_min, n_min)))
        qmp.click(84, 1008)               # 再点恢复
        time.sleep(1.5)
        qmp.shot(S[11])
        _, _, df3 = ppm_load(S[11])
        d_res, n_res = frame_diff(df2, df3, w, h, step=4)
        results.append(("F: taskbar button restores Files",
                        d_res * 5 > n_res,
                        "| diff %d/%d" % (d_res, n_res)))
        results.append(("F: gui alive", alive(qmp, S, w, h, 12)))
        # F 收尾：点 Notepad 露出底缘 (496,727) 让它 focused=最上再逆序关
        qmp.click(496, 727)
        time.sleep(0.5)
        wins.close_all(qmp, S, w, h)
        qmp.shot(S[13])
        _, _, dfinal = ppm_load(S[13])
        results.append(("F: final cleanup leaves no window",
                        not is_page_white(px(dfinal, w, 200, 200)),
                        "| desk=%r" % (px(dfinal, w, 200, 200),)))

        # report
        print("\n===== probe_combo results =====")
        allok = True
        for r in results:
            name = r[0]
            okv = r[1]
            detail = r[2] if len(r) > 2 else ""
            print("%s %s%s" % ("PASS" if okv else "FAIL", name,
                               (" | " + detail) if detail else ""))
            allok = allok and okv
        print("\nALL PASS" if allok else "\nHAS FAILURES")
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
        for p in S:
            if os.environ.get("PROBE_KEEP") != "1":
                rm(p)


if __name__ == "__main__":
    sys.exit(main())
