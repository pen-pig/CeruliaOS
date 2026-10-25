# -*- coding: utf-8 -*-
"""mut_drive.py - 变异测试驱动器：改内核 -> 重建 -> 跑小探针 -> 必须变红 -> 还原

用法：
    python tests/mut_drive.py M1 M2 M3 M4      # 依次跑这四个变异

每个变异：
  1. 把 kernel/gfxwin.c 的一处代码换成"坏版本"（注释/常量级改动）
  2. gen_ninja + ninja 重建 os-image.bin
  3. 跑 tests/dbg_mut.py（3~4 分钟，只覆盖被变异的行为）
  4. **期望**：对应那条断言变红，其余保持绿（变异精确 = 闸门有牙且不误伤）
  5. 还原源码并重建（无论第 3 步结果如何都还原）

红/绿判定：dbg_mut.py 的输出行 "PASS <名字>" / "FAIL <名字>"。
"""
import io
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
KERNEL = os.path.join(ROOT, "kernel", "gfxwin.c")

# name -> (期望变红的断言前缀, 原代码, 变异代码)
MUTATIONS = {
    # M1：标题栏最小化钮失效（gw_title_btn_hit 的 btn==1 分支被关掉）
    "M1": ("M1: titlebar-min hides the window",
           "else if (title_btn == 1) { gw_minimize(hit); gw_tbtn_arm(mx, my); }",
           "else if (0 && title_btn == 1) { gw_minimize(hit); gw_tbtn_arm(mx, my); }"),
    # M2：三连击连触发（去掉 last_title_wnd = NULL 的"防三击"复位）
    "M2": ("M2: triple-click toggles maximize only once",
           "                last_title_wnd = NULL;   /* 防三击连触发 */",
           "                /* MUT: last_title_wnd = NULL; */"),
    # M3：cascade y 步进 46 -> 44（gw_scale_y(22)=46 改成 gw_scale_y(21)=44）
    "M3": ("M3: cascade step is x+56 / y+46",
           "int wy = gw_scale_y(24) + (cascade % 4) * gw_scale_y(22);",
           "int wy = gw_scale_y(24) + (cascade % 4) * gw_scale_y(21);"),
    # M4：主题 accent 派生写死成默认蓝（16bpp 标题栏不再跟随主题）
    # 注意：变异体必须仍能编译过 -Werror——老写法把 a 闲置成 unused
    # variable，编译直接失败，等于没测。改成"用 a 但结果恒为默认蓝"。
    "M4": ("M4: theme accent drives the 16bpp title bar color",
           "    uint32_t a = gw_accent;\n"
           "    gw_accent_dark = (a * 3) / 4;                  /* 暗一档 */\n"
           "    gw_accent_light = a + (0xFFFFFFu - a) / 3;     /* 亮一档 */",
           "    uint32_t a = gw_accent;   /* MUT: a 仍在用，避免 unused 编译错 */\n"
           "    gw_accent_dark = CLR_BLUE_DARK + (a - a);      /* MUT: 恒默认蓝 */\n"
           "    gw_accent_light = CLR_BLUE_LIGHT + (a - a);    /* MUT: 恒默认蓝 */"),
    # M5：拖动态改回老的 ±8/±16 容差盒（不是指针抓取）-> 单包位移大于
    #     容差时第一个包就掉出，窗口一步不动（快速拖动掉窗）
    "M5": ("M5: fast drag (24px per packet) keeps following the cursor",
           "            if (!(buttons & 1)) {\n"
           "                /* 按钮已松：清残留并吞掉本次释按。不能让它继续往下走——\n"
           "                 * 否则释按会被当成一次内容区 click 派发给光标下的窗口 */\n"
           "                gw_end_drags();\n"
           "                last_btn = buttons;\n"
           "                return;\n"
           "            }\n"
           "            hit = w;\n"
           "            break;",
           "            if (my >= w->y - 8 && my < w->y + GW_TITLE_H + 8 &&\n"
           "                mx >= w->x - 16 && mx < w->x + w->w + 16) {\n"
           "                hit = w; break;      /* MUT: 容差盒 */\n"
           "            }"),
    # M7：桌面层局部重绘退回全屏（gw_redraw_desktop_region -> gw_draw_desktop）
    #     -> 拖动时每帧先把 260 万像素的整屏桌面擦一遍再画窗口，
    #        screendump/肉眼看到"桌面闪过 -> 窗口才出现"的半成品帧
    #        （dbg_flicker 首轮实测：24 帧 13 帧不完整、匹配率最低 0.19、
    #        帧间 diff 峰值 47 万）。dbg_mut M7 的匹配率+跟手判据应变红。
    #     只改函数体第一句：保留函数定义避免 "defined but not used"
    #     （-Werror 直接编译失败，等于没测——M4/M6 踩过两次）。
    #     附注：M6（去掉 blit 快路径、保留局部桌面重绘）实测**不**产生
    #     可观测差异（probe_flicker 全量 20+16 帧 worst-match=1.00、
    #     diff-peak 3.1 万），blit 是纯性能优化而非闪动修复，故不设闸门。
    "M7": ("M7: drag leaves no half-drawn frame",
           "static void gw_redraw_desktop_region(int x, int y, int w, int h)\n"
           "{\n"
           "    int mx, my;\n"
           "\n"
           "    if (w <= 0 || h <= 0) return;\n"
           "    mx = mouse_get_x(); my = mouse_get_y();\n"
           "\n"
           "    if (gw_theme_gnome()) { gw_draw_desktop(); return; }",
           "static void gw_redraw_desktop_region(int x, int y, int w, int h)\n"
           "{\n"
           "    int mx, my;\n"
           "\n"
           "    if (w <= 0 || h <= 0) return;\n"
           "    gw_draw_desktop(); return;   /* MUT: 全屏桌面 */\n"
           "    mx = mouse_get_x(); my = mouse_get_y();\n"
           "\n"
           "    if (gw_theme_gnome()) { gw_draw_desktop(); return; }"),
}


def sh(cmd, cwd=ROOT):
    p = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("latin-1", "replace")


def build():
    rc, out = sh([sys.executable, "tools/gen_ninja.py", "--src", "build.ninja",
                  "--out", "build_auto.ninja"])
    if rc != 0:
        print("gen_ninja FAILED:\n" + out[-3000:], flush=True)
        return False
    rc, out = sh([os.path.join(ROOT, "ninja.exe"), "-f", "build_auto.ninja",
                  "-j4", "os-image.bin"])
    if rc != 0:
        print("ninja FAILED:\n" + out[-3000:], flush=True)
        return False
    print("[build] ok", flush=True)
    return True


def run_probe():
    rc, out = sh([sys.executable, "tests/dbg_mut.py"])
    lines = [l for l in out.splitlines() if l.startswith("PASS ") or
             l.startswith("FAIL ")]
    for l in lines:
        print("   " + l[:150], flush=True)
    return lines


def main():
    names = sys.argv[1:] or list(MUTATIONS)
    src0 = io.open(KERNEL, encoding="utf-8").read()
    allok = True
    try:
        for name in names:
            if name not in MUTATIONS:
                print("unknown mutation %s" % name, flush=True)
                allok = False
                continue
            want, old, new = MUTATIONS[name]
            if old not in src0:
                print("=== %s: ANCHOR NOT FOUND (内核已变?) ===" % name, flush=True)
                print(old[:120], flush=True)
                allok = False
                continue
            print("\n=== 变异 %s ===" % name, flush=True)
            print("   期望变红: %s" % want, flush=True)
            io.open(KERNEL, "w", encoding="utf-8").write(src0.replace(old, new, 1))
            try:
                if not build():
                    allok = False
                    continue
                lines = run_probe()
                red = [l for l in lines if l.startswith("FAIL ") and
                       l[5:].split(":")[0] == want.split(":")[0]]
                others_red = [l for l in lines if l.startswith("FAIL ") and
                              l[5:].split(":")[0] != want.split(":")[0]]
                if red:
                    print("   => OK: 目标断言变红%s" %
                          ("（另有 %d 条误伤红: %r）" % (len(others_red), others_red)
                           if others_red else "，无误伤"), flush=True)
                else:
                    print("   => FAIL: 目标断言**没有变红**（闸门无牙）", flush=True)
                    allok = False
            finally:
                io.open(KERNEL, "w", encoding="utf-8").write(src0)
                print("   [restore] 源码已还原", flush=True)
                build()
    finally:
        io.open(KERNEL, "w", encoding="utf-8").write(src0)
    print("\n===== 变异测试总结: %s =====" % ("ALL OK" if allok else "HAS PROBLEM"))
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
