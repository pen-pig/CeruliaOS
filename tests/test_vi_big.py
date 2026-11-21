# -*- coding: utf-8 -*-
"""test_vi_big.py - vi / cat / wc / tail 大文件闭环（>4KB 全量读写）。

背景：2026-10-18 前，shell 的 cat/grep/wc/head/tail/cp/mv/vi 各自持有 4KB
static 缓冲，vi 行列 100x96=9.6KB，追加重定向 8KB。任何超过 4KB 的文档都被
**静默截断**——vi 打开 20KB 文件只读前 4KB、存盘也只存前 4KB，cat/wc/tail
同样只看到前 4KB。这是"日用编辑文档"的第一道墙。

本测试钉行为（不钉实现）：
  1. 宿主造一个 20KB / 200 行的 exFAT 文件（ref_exfat 独立实现）
  2. wc -l 必须数出全部 200 行          （读路径无截断）
  3. tail 必须显示最后一行 END 标记      （尾部字节在缓冲里）
  4. vi 打开 -> G 跳尾 -> 屏幕有 END 标记（vi 加载全量）
  5. vi 尾部插入一行 -> :wq -> 宿主复检  （存路径无截断，头部字节不变）

用法：python tests/test_vi_big.py   （退出码 0 = 全通过）
端口由 alloc_port() 动态分配（端口必须每脚本唯一，见 tests/README.md）。
"""
import os as _os_ez
import sys as _sys_ez
_sys_ez.path.insert(0, _os_ez.path.dirname(_os_ez.path.abspath(__file__)))
from ezos_qemu import alloc_port, image_path, disk_path, log_path, kill_stale_qemu
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.append(ROOT)
from ezos_env import qemu_exe  # noqa: E402

import test_rmdir as T
from test_rmdir import Qmp  # noqa: F401
from ref_exfat import ExfatRW  # noqa: E402

QEMU = qemu_exe()

NAME = "BIGDOC.C"
NLINES = 200
LINELEN = 105
END_MARK = "ZQEND9Z"          # 最后一行独有的标记，OCR 不会认错
INSERT_MARK = "qqtail7qq"     # vi 里插入的行标记（sendkey 只能发小写）

RESULTS = []


def check(ok, label):
    RESULTS.append((bool(ok), label))
    print(("PASS " if ok else "FAIL ") + label)


def make_big_file(disk):
    """宿主侧在 exFAT 数据盘上造 20KB/200 行文件，返回 (总字节, 末行)。"""
    rw = ExfatRW(disk)
    lines = []
    for i in range(NLINES):
        if i == NLINES - 1:
            tail = END_MARK + " line %d " % i
            lines.append(tail + "x" * (LINELEN - len(tail)))
        else:
            lines.append("row%03d " % i + "abcdefghij" * 9 + " tail%03d" % i)
        assert len(lines[-1]) == LINELEN, (i, len(lines[-1]))
    data = ("\n".join(lines) + "\n").encode()
    rw.create(rw.root, NAME, data)
    rw.flush()
    return len(data), lines[-1]


def main():
    disk = os.path.join(HERE, "disk_vi_big.vhd").replace("\\", "/")
    shutil.copyfile(os.path.join(ROOT, "disk.vhd"), disk)
    img = image_path()

    total, last_line = make_big_file(disk)
    print("host: wrote %s = %d bytes, %d lines, last=%r" % (NAME, total, NLINES, last_line[:20]))

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

        # 1) wc 数全行（读路径无截断）。本 shell 的 wc 无 -l 选项，
        #    固定输出 "lines words chars name"，取第一个数字。
        out = T.last_out(T.run(qmp, "wc " + NAME, 20.0)).lower()
        nums = [t for t in out.replace(",", " ").split() if t.isdigit()]
        ok1 = bool(nums) and int(nums[0]) == NLINES
        check(ok1, "wc 数出全部 %d 行（读无截断）: %r" % (NLINES, out[-40:]))
        if not ok1:
            fails += 1

        # 2) tail 显示最后一行（尾部字节在缓冲里）
        out = T.last_out(T.run(qmp, "tail " + NAME, 20.0)).lower()
        ok2 = END_MARK.lower() in out
        check(ok2, "tail 显示末行标记 %s" % END_MARK)
        if not ok2:
            fails += 1

        # 3) vi 打开 -> G 跳尾 -> 屏幕出现末行（vi 加载全量）
        T.run(qmp, "vi " + NAME, 15.0)
        time.sleep(1.0)
        qmp.hmc("sendkey shift-g")
        time.sleep(1.5)
        scr = qmp.screen().lower()
        ok3 = END_MARK.lower() in scr
        check(ok3, "vi G 跳尾后屏幕有末行标记")
        if not ok3:
            fails += 1

        # 4) vi 尾部插入一行 -> :wq（存路径无截断）
        qmp.hmc("sendkey o")
        time.sleep(0.3)
        for ch in INSERT_MARK:
            qmp.hmc("sendkey " + ("spc" if ch == ' ' else ch.lower()))
            time.sleep(0.05)
        qmp.hmc("sendkey esc")
        time.sleep(0.3)
        qmp.hmc("sendkey shift-semicolon")
        time.sleep(0.2)
        qmp.hmc("sendkey w")
        qmp.hmc("sendkey q")
        qmp.hmc("sendkey ret")
        time.sleep(3.0)

        # 5) 宿主复检：插入行在文件末尾，头部 200 行原样未动
        from ref_exfat import Exfat
        back = Exfat(disk).read_path(NAME)
        head_ok = back.startswith(b"row000 ")
        tail_ok = INSERT_MARK.encode() in back[-64:]
        grew_ok = len(back) == total + len(INSERT_MARK) + 1
        check(head_ok, "宿主复检：头部首行未损坏")
        check(tail_ok, "宿主复检：插入行落在文件末尾")
        check(grew_ok, "宿主复检：文件长度 %d -> %d（+%d，无截断）" % (total, len(back), len(back) - total))
        print("  dbg back[-60:] = %r" % back[-60:])
        if not (head_ok and tail_ok and grew_ok):
            fails += 1

        # 6) 退出 vi 后 shell 仍可用
        out = T.last_out(T.run(qmp, "ver", 10.0)).lower()
        ok6 = "version" in out
        check(ok6, "退出 vi 后 shell 仍可用")
        if not ok6:
            fails += 1

        print("== vi big-file: %d/%d passed ==" % (len(RESULTS) - fails, len(RESULTS)))
        return 1 if fails else 0
    finally:
        proc.kill()
        try:
            proc.wait(timeout=15)
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
