# -*- coding: utf-8 -*-
"""test_wm.py - 图形桌面窗口管理器回归（用户报的三症状：拖不动 / 闪 / 主题异常）

背景（2026-10-18）：gfx_init 的 VBE LFB 路径被一行 `0 &&`
（TEMP-FORCE-FALLBACK 调试残留）永久短路，desktop 命令落到 VGA 0x13
fallback；而 QEMU cirrus 在 DISPI 仍 enabled 时忽略标准 VGA 模式切换
（寄存器回读全对、屏幕不动），内核按 320x200 往 0xA0000 画 →
桌面全不可见、鼠标命中全错位、每帧重绘闪烁。用户侧现象就是
"窗口拖不动 / 屏幕一直闪 / 主题不正常"。

本测试把三个症状钉成断言：
  1. desktop 必须进 boot 探测的 VBE 几何（1280x1024）——退到 0x13
     回退（320x200）或文本模式（640x480）都直接红
  2. 无输入连拍两帧，帧间差异 < 1%——闪屏红
  3. 标题栏拖动 120px，窗口 bbox 必须真的移动——拖不动红
     （含竖向快拖：8px 步进超过标题栏高度时续命判据必须容错，否则
      竖向拖动必死——2026-10-18 真 bug，用户"有时拖得动有时拖不动"）
  4. 任务栏暗色 + 标题栏 accent 蓝——主题错红
  5. 左边缘钳制：把窗口一路拖出左屏，必须钳到 x=0 并 1:1 跟随回拖——
     2026-10-18 二次 bug：桌面图标拦截在拖动中吞事件，窗口死在图标列
     右缘 x=124（光标在图标列里→事件被图标吞→窗口收不到位移→永远
     盖不住图标→永远被吞），且释按落在容差外会残留拖动态
  6. 任务栏上释按不残留拖动态：换偏移重新按下微拖，窗口必须精确跟随
     （陈旧 move_dx 复用会跳 +45px）

用法：
    python tests/test_wm.py            # BIOS 路径
    python tests/test_wm.py uefi       # UEFI 路径
    python tests/test_wm.py bios:std   # 指定 -vga 型号
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
LOG = os.path.join(ROOT, "temp", "wm_serial.log").replace("\\", "/")
QMP_PORT = alloc_port()
BOOT_WAIT = 90.0

# 断言锚点（2026-10-18 实测，改桌面配色/几何时要跟着重锚）：
#   任务栏带平均 (14,25,21)，标题栏 accent 蓝 (0,120,215) 9878/10092 像素，
#   标题栏右无字区 708/720 像素是 accent 蓝。
TB_MAX_CHANNEL = 60      # 任务栏任一通道均值上限（暗色）
TITLE_BLUE_MIN = 400     # 标题栏右无字区 accent 蓝像素下限（实测 708）


def rec(results, name, ok, detail=""):
    results.append((name, bool(ok)))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       (" | " + detail) if detail else ""))


def rm(path):
    """沙箱会拦 os.remove：一切删除带保护，否则断言全过后炸 finally
    （现象"最后一条 PASS 却退出码 1"）"""
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
        for i, ch in enumerate(text):
            self.hmc("sendkey " + ("spc" if ch == " " else ch))
            time.sleep(0.03)
            if i % 10 == 9:
                time.sleep(0.15)
        self.hmc("sendkey ret")
        time.sleep(3.0)

    def key(self, k):
        self.hmc("sendkey " + k)
        time.sleep(0.2)

    def move_to(self, x, y):
        while self.cx != x or self.cy != y:
            dx = max(-12, min(12, x - self.cx))
            dy = max(-12, min(12, y - self.cy))
            self.hmc("mouse_move %d %d" % (dx, dy))
            self.cx += dx
            self.cy += dy
            time.sleep(0.05)
        time.sleep(0.15)

    def drag_move(self, dx, dy, steps=1, pause=0.04):
        """按住状态下的相对位移。与裸 hmc("mouse_move") 的区别是同步
        坐标簿——否则后续 move_to 会按失步的簿子发相对位移，点错地方"""
        for _ in range(steps):
            self.hmc("mouse_move %d %d" % (dx, dy))
            self.cx += dx
            self.cy += dy
            time.sleep(pause)

    def shot(self, path):
        rm(path)
        self.hmc("screendump " + path)
        time.sleep(0.4)
        return path


def ppm_load(fn):
    with open(fn, "rb") as f:
        data = f.read()
    parts = data.split(b"\n", 3)
    w, h = map(int, parts[1].split())
    return w, h, parts[3]


def win_bbox(buf, w, h):
    """窗口内容底 #F3F3F3/白 的大块亮区 -> (x0,y0,x1,y1)；找不到返回 None。
    用"最长连续亮色 Run"定位，桌面图标（<=56px 宽）不会污染。"""
    best = None
    for y in range(0, h - 30):          # 排除底部任务栏
        base = y * w
        run = 0
        for x in range(w):
            o = (base + x) * 3
            if buf[o] > 225 and buf[o + 1] > 225 and buf[o + 2] > 225:
                run += 1
                if best is None or run > best[0]:
                    best = (run, x - run + 1, x, y)
            else:
                run = 0
    if best is None or best[0] < 200:
        return None
    _, x0, x1, yrow = best
    # 上下扩展：同一 x 区间内仍是亮色的行
    y0 = y1 = yrow
    while y0 - 1 >= 0:
        o = ((y0 - 1) * w + (x0 + x1) // 2) * 3
        if buf[o] > 225 and buf[o + 1] > 225 and buf[o + 2] > 225:
            y0 -= 1
        else:
            break
    while y1 + 1 < h:
        o = ((y1 + 1) * w + (x0 + x1) // 2) * 3
        if buf[o] > 225 and buf[o + 1] > 225 and buf[o + 2] > 225:
            y1 += 1
        else:
            break
    return (x0, y0, x1, y1)


def title_grab(bb, win_h, dx=3):
    """标题栏抓取点（x=窗口左缘+dx, y=标题栏中心）。
    bbox 是内容区：内容顶 = 底边-窗高，窗框外顶 = 内容顶-13，
    标题栏中心 = 内容顶-7。用底边反推是因为顶边锚点会被终端内容里的
    文字打断（2026-10-18 实测 y0 假性 +54），底边永远干净"""
    return (bb[0] + dx, bb[3] - win_h - 7)


def frame_diff(a, b, w, h, step=3):
    n = d = 0
    for y in range(0, h, step):
        base = y * w
        for x in range(0, w, step):
            o = (base + x) * 3
            n += 1
            if a[o:o + 3] != b[o:o + 3]:
                d += 1
    return d, n


def zone_avg(buf, w, x0, y0, x1, y1, step=4):
    r = g = b = n = 0
    for y in range(y0, y1, step):
        for x in range(x0, x1, step):
            o = (y * w + x) * 3
            n += 1
            r += buf[o]; g += buf[o + 1]; b += buf[o + 2]
    return (r // n, g // n, b // n)


def accent_blue_count(buf, w, h, x0, y0, x1, y1):
    """accent 蓝 (0,120,215) 容差 +-60 的像素数"""
    n = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            o = (y * w + x) * 3
            if (abs(buf[o] - 0) <= 60 and abs(buf[o + 1] - 120) <= 60
                    and abs(buf[o + 2] - 215) <= 60):
                n += 1
    return n


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "bios"
    vga = "std"
    if path.startswith("bios:"):
        vga = path.split(":", 1)[1]
        path = "bios"
    results = []
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    rm(LOG)
    if path == "uefi":
        from ezos_env import qemu32_exe, ovmf_fd
        ESP = os.path.join(ROOT, "uefi/esp").replace("\\", "/")
        DISK_SRC = os.path.join(ROOT, "disk.vhd").replace("\\", "/")
        DISK2 = os.path.join(ROOT, "temp", "wm_disk.vhd").replace("\\", "/")
        import shutil
        shutil.copyfile(DISK_SRC, DISK2)
        proc = subprocess.Popen(
            [qemu32_exe(), "-machine", "pc", "-m", "256", "-vga", "std",
             "-icount", "shift=auto",
             "-drive", "if=pflash,format=raw,readonly=on,file=" + ovmf_fd(),
             "-drive", "if=none,format=raw,file=fat:rw:" + ESP + ",id=esp",
             "-usb", "-device", "usb-storage,drive=esp",
             "-drive", "format=raw,file=" + DISK2,
             "-serial", "file:" + LOG,
             "-display", "none",
             "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % QMP_PORT],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc = subprocess.Popen(
            [QEMU, "-machine", "pc", "-vga", vga, "-m", "128",
             "-drive", "format=raw,file=" + IMG,
             "-drive", "format=raw,file=" + DISK,
             "-display", "none", "-no-reboot",
             "-serial", "file:" + LOG,
             "-qmp", "tcp:127.0.0.1:%d,server,nowait" % QMP_PORT],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    qmp = None
    S = [os.path.join(ROOT, "temp", "wm_s%d.ppm" % i).replace("\\", "/")
         for i in range(14)]
    try:
        # ---- boot ----
        end = time.time() + BOOT_WAIT
        ok_shell = False
        while time.time() < end:
            try:
                if "[/] > " in open(LOG, "rb").read().decode("latin-1", "replace"):
                    ok_shell = True
                    break
            except OSError:
                pass
            time.sleep(0.5)
        rec(results, "boot reaches shell", ok_shell)
        if not ok_shell:
            return 1
        qmp = Qmp(QMP_PORT)
        if path == "uefi":
            # 吃 OVMF 键盘残留字节（同 test_uefi3）：否则首个命令被贴上杂字符
            qmp.line("ver")
            time.sleep(1.5)

        qmp.line("desktop")
        time.sleep(35)                    # 开机动画 + 首帧（UEFI TCG 更慢）
        qmp.shot(S[0])
        w, h, d0 = ppm_load(S[0])
        # 几何钉死：boot 探测的 VBE 模式。退 0x13（320x200）或文本（640x480）都红
        rec(results, "desktop entered at VBE geometry", (w, h) == (1280, 1024),
            "| %dx%d (expect 1280x1024)" % (w, h))
        if (w, h) != (1280, 1024):
            return 1
        # gw_start 的 mouse_warp 把光标放在屏幕正中心——QMP 坐标簿以此为准
        qmp.cx, qmp.cy = w // 2, h // 2

        # ---- open Terminal via keyboard shortcut ----
        qmp.key("t")
        time.sleep(2.5)
        qmp.shot(S[1])
        w, h, d1 = ppm_load(S[1])
        bb1 = win_bbox(d1, w, h)
        rec(results, "terminal window visible", bb1 is not None,
            "| bbox=%r" % (bb1,))
        if bb1 is None:
            return 1
        x0, y0, x1, y1 = bb1
        print("  window bbox: %r  (w=%d h=%d)" % (bb1, x1 - x0, y1 - y0))

        # ---- flicker: no input, two shots ----
        qmp.shot(S[2])
        time.sleep(1.2)
        qmp.shot(S[3])
        w2, h2, d2 = ppm_load(S[2])
        _, _, d3 = ppm_load(S[3])
        d, n = frame_diff(d2, d3, w, h)
        rec(results, "no-input frames stable (flicker)", d * 100 < n,
            "| diff %d/%d = %.2f%%" % (d, n, d * 100.0 / n))

        # ---- drag: title bar, +120px right ----
        # 标题栏中心 = 内容顶-7（GW_TITLE_H=12）；抓取点用底边反推保稳
        win_h = y1 - y0
        tx, ty = title_grab(bb1, win_h, dx=(x1 - x0) // 2)
        qmp.move_to(tx, ty)
        qmp.hmc("mouse_button 1")
        time.sleep(0.3)
        qmp.drag_move(12, 0, steps=10, pause=0.06)
        time.sleep(0.5)
        qmp.hmc("screendump " + S[4])
        time.sleep(0.4)
        wM, hM, dM = ppm_load(S[4])
        bb2 = win_bbox(dM, wM, hM)
        rec(results, "window still visible mid-drag", bb2 is not None,
            "| bbox=%r" % (bb2,))
        if bb2 is None:
            return 1
        qmp.hmc("mouse_button 0")
        time.sleep(1.0)
        qmp.shot(S[5])
        w5, h5, d5 = ppm_load(S[5])
        bb3 = win_bbox(d5, w5, h5)
        rec(results, "window still visible after release", bb3 is not None,
            "| bbox=%r" % (bb3,))
        if bb3 is None:
            return 1
        dx = bb3[0] - bb1[0]
        rec(results, "window moved right ~120px", 80 <= dx <= 160,
            "| dx=%d (expect ~120)" % dx)

        # ---- vertical drag with fast steps (regression) ----
        # 真 bug（2026-10-18）：拖动续命判据 my < hit->y+GW_TITLE_H 没有容差，
        # 而优先命中判据有 ±8——鼠标单帧步进超过标题栏高度（竖向快拖 8px）
        # 时光标落到标题栏下方，事件掉进内容区分支，拖动当场中断且再也回不来。
        # 用户侧现象"有时拖得动有时拖不动"，竖向快拖必现；横向不受影响
        # （分支判据从不限制 x）。修法：moving 时续命判据含容差。
        # dy 用底边差量算：y1 不受终端文字污染，y0 会（实测假性 +54）
        qmp.move_to(w - 180, h - 200)
        qmp.shot(S[6])
        _, _, d6 = ppm_load(S[6])
        bb_v0 = win_bbox(d6, w, h)
        if bb_v0 is None:
            rec(results, "clean bbox before vertical drag", False, "| bbox=None")
            return 1
        gx, gy = title_grab(bb_v0, win_h, dx=(bb_v0[2] - bb_v0[0]) // 2)
        qmp.move_to(gx, gy)
        qmp.hmc("mouse_button 1")
        time.sleep(0.3)
        qmp.drag_move(0, 8, steps=15)        # 竖向快拖 120px（8px 步进）
        time.sleep(0.3)
        qmp.hmc("mouse_button 0")
        time.sleep(1.0)
        qmp.move_to(w - 180, h - 200)
        qmp.shot(S[7])
        _, _, d7 = ppm_load(S[7])
        bb_v1 = win_bbox(d7, w, h)
        vdy = (bb_v1[3] - bb_v0[3]) if bb_v1 else -999
        rec(results, "vertical drag follows fast steps",
            bb_v1 is not None and 80 <= vdy <= 160,
            "| dy=%d (expect ~120; stall bug = 0)" % vdy)

        # ---- theme: taskbar dark + titlebar accent blue ----
        tb = zone_avg(d1, w, 0, h - 26, w, h - 2)
        dark_ok = max(tb) < TB_MAX_CHANNEL
        rec(results, "taskbar dark", dark_ok,
            "| avg=%r (max channel < %d)" % (tb, TB_MAX_CHANNEL))
        # 标题栏右端无字区（避开标题文字）：y0-13..y0-1, x1-80..x1-20
        blue = accent_blue_count(d1, w, h, x1 - 80, y0 - 13, x1 - 20, y0 - 1)
        rec(results, "titlebar accent blue", blue > TITLE_BLUE_MIN,
            "| %d px (expect > %d)" % (blue, TITLE_BLUE_MIN))

        # ---- clamp: drag left past the icon column (deadlock regression) ----
        # 真 bug（2026-10-18）：桌面图标双击拦截在拖动中吞事件。Win10 主题
        # 无 dock，图标列在 x=16..122；光标进图标列→事件被图标吞→窗口收不到
        # 位移→永远盖不住图标→永远被吞。实测窗口死在 x=124（图标列右缘），
        # 应一路钳到 gw_desktop_left()=0。修法：拖动中桌面层拦截全让位。
        # 先把光标移到窗口外再量 bbox：光标精灵压在窗顶会污染顶边锚点
        qmp.move_to(w - 180, h - 200)
        qmp.shot(S[8])
        _, _, d6 = ppm_load(S[8])
        bb_clean = win_bbox(d6, w, h)
        if bb_clean is None:
            rec(results, "clean bbox before clamp drag", False, "| bbox=None")
            return 1
        cx, cy = title_grab(bb_clean, win_h)
        qmp.move_to(cx, cy)
        qmp.hmc("mouse_button 1")
        time.sleep(0.3)
        qmp.drag_move(-8, 0, steps=40)      # 一路向左出屏（QEMU 把指针钳在 0）
        time.sleep(0.5)
        qmp.hmc("screendump " + S[9])
        time.sleep(0.4)
        _, _, d7 = ppm_load(S[9])
        bb4 = win_bbox(d7, w, h)
        rec(results, "drag clamps at desktop left edge",
            bb4 is not None and bb4[0] <= 2,
            "| bbox=%r (expect x0<=2; deadlock bug stuck at x=124)" % (bb4,))
        # 同一笔拖动继续向右。回拖一半时光标还在图标列里（x≈80）：
        # 修复后窗口必须已跟上光标；死锁态窗口仍冻在 124 → 红
        qmp.drag_move(8, 0, steps=10)
        time.sleep(0.4)
        qmp.hmc("screendump " + S[10])
        time.sleep(0.4)
        _, _, d8 = ppm_load(S[10])
        bbM = win_bbox(d8, w, h)
        rec(results, "window tracks mouse over icon column",
            bbM is not None and bbM[0] <= 90,
            "| bbox=%r (expect x0<=90 mid-return)" % (bbM,))
        qmp.drag_move(8, 0, steps=10)       # 回拖满 160px
        time.sleep(0.3)
        qmp.hmc("mouse_button 0")
        time.sleep(1.0)
        qmp.shot(S[11])
        _, _, d9 = ppm_load(S[11])
        bb5 = win_bbox(d9, w, h)
        # 钳制期间 move_dx 已按新位置重校准，回拖应 1:1：0 -> 160
        rec(results, "window follows mouse 1:1 after clamp",
            bb5 is not None and 120 <= bb5[0] <= 200,
            "| bbox=%r (expect x0 in 120..200)" % (bb5,))

        # ---- stuck-drag regression: 拖到底后在任务栏上释按 ----
        # 真 bug：释按落在 gw_handle_desktop_mouse 的拦截区（任务栏/开始菜单/
        # GNOME Dock）时 gw_handle_mouse 根本不会被调用，旧代码只在标题栏分支
        # 里清 moving → 拖动态残留：主循环每帧按 dragging 合并重绘 + 下次按下
        # 复用陈旧 move_dx → 窗口跳位。修法：释按即终 drag（gw_end_drags）。
        gx, gy = title_grab(bb5, win_h)
        qmp.move_to(gx, gy)
        qmp.hmc("mouse_button 1")
        time.sleep(0.3)
        qmp.drag_move(12, 0, steps=5)             # 先右拖 60px
        qmp.move_to(qmp.cx, h - 10)               # 光标下移压到任务栏（窗口钳底边）
        time.sleep(0.3)
        qmp.hmc("mouse_button 0")                 # 在任务栏上释按
        time.sleep(1.0)
        qmp.shot(S[12])
        _, _, d10 = ppm_load(S[12])
        bb6 = win_bbox(d10, w, h)
        rec(results, "window survives taskbar release", bb6 is not None,
            "| bbox=%r" % (bb6,))
        if bb6 is None:
            return 1
        # 换一个抓取偏移（x+40）重新按下并微拖 8px：修复后窗口精确跟随（+8）；
        # 拖动态残留则复用陈旧 move_dx（=3）→ 窗口跳到 +45 → 红
        gx, gy = title_grab(bb6, win_h, dx=40)
        qmp.move_to(gx, gy)
        qmp.hmc("mouse_button 1")
        time.sleep(0.3)
        qmp.drag_move(8, 0, steps=1)
        time.sleep(0.4)
        qmp.hmc("screendump " + S[13])
        time.sleep(0.4)
        _, _, d11 = ppm_load(S[13])
        bb7 = win_bbox(d11, w, h)
        rec(results, "no phantom-drag jump after taskbar release",
            bb7 is not None and 4 <= bb7[0] - bb6[0] <= 16,
            "| dx=%d (expect ~8; stale move_dx bug jumps +45)"
            % (bb7[0] - bb6[0] if bb7 else -999))
        qmp.hmc("mouse_button 0")
        time.sleep(0.5)
        # 坐标簿重同步：前面出屏/任务栏段 QEMU 钳过指针，簿子已失真；
        # 大幅右推把实际指针顶到右缘，按钳制后的确定值重记账
        qmp.drag_move(200, 0, steps=6, pause=0.02)
        qmp.cx, qmp.cy = w - 1, qmp.cy

        failed = [n for n, ok in results if not ok]
        print("\n%d/%d passed" % (len(results) - len(failed), len(results)))
        for n in failed:
            print("  FAILED: " + n)
        return 1 if failed else 0
    finally:
        if qmp is not None:
            try:
                qmp.cmd("quit")
            except Exception:
                pass
        time.sleep(1)
        try:
            proc.kill()
        except OSError:
            pass
        for p in S + [LOG, os.path.join(ROOT, "temp", "wm_disk.vhd").replace("\\", "/")]:
            if not os.environ.get("WM_KEEP"):
                rm(p)


if __name__ == "__main__":
    sys.exit(main())
