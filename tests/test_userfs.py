# -*- coding: utf-8 -*-
"""test_userfs.py - 用户态文件系统能力 E2E（SYS_READDIR/SYS_STAT + libc）

背景：syscall 面原先只有 read/write/open/close/lseek，用户态程序"没有
目录可看、没有属性可查"，libc 只有 puts/putdec——写实际应用的最低门槛
都不够。本测试守护补上来的四件能力，全部经由用户程序 dirwalk 自检后
打串口，E2E 按子串断言（不依赖 OCR）：

  A. `exec dirwalk`（/bin 走内置系统卷，不依赖数据盘）：
     - printf 全规格输出逐字节一致（d=-42 u=42 x=2a X=2A ...）
     - malloc 两块不重叠、合并后原地重用、malloc(huge) 拒绝、
       二次释放被拒后堆仍可用
     - readdir /bin 列条目、hello.elf 有 size 且非目录；
       readdir 不存在目录 / readdir 一个文件都必须 -1
     - stat 文件给 size、stat /bin 给 dir=1、stat 不存在 -1
  B. 数据盘侧：shell `write` 造一个 8 字节文件，
     `exec dirwalk /D1.TXT` 必须 stat 出 size=8（覆盖磁盘后端而不只是
     内置系统卷）。
  C. 收尾：exec 退出码 0、shell 仍可用（ver）。

反证不靠"再写一个坏内核"：dirwalk 内部的负例（stat missing rejected 等）
就是反证——内核若把不存在路径当成存在、或把文件当成目录返回，这些
PASS 行会变 FAIL，"ALL OK" 不会出现，A 组整组红。
提交前另做内核变异验证（见 tests/README 记录）：把 fs_stat 的存在性
检查注掉重建，本测试必须红。

用法：python tests/test_userfs.py   （退出码 0 = 全通过）
端口由 alloc_port() 动态分配（端口必须每脚本唯一，见 tests/README.md）。
"""
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from ezos_env import qemu_exe  # noqa: E402
from ezos_qemu import alloc_port, image_path, disk_path, kill_stale_qemu  # noqa: E402
from test_nvme import SerialReader, Qmp, wait_for, run_cmd  # noqa: E402
import test_rmdir as T  # noqa: E402

QEMU = qemu_exe()
BOOT_WAIT = 150

RESULTS = []


def check(ok, label):
    RESULTS.append((bool(ok), label))
    print(("PASS " if ok else "FAIL ") + label)


def main():
    img = image_path()
    disk = disk_path()      # 每进程私有副本，不污染基线盘

    qmp_port, ser_port = alloc_port(2)
    if T.port_in_use(qmp_port) or T.port_in_use(ser_port):
        kill_stale_qemu("qemu-system-x86_64.exe")
        T.wait_port_free(qmp_port, 15)
        T.wait_port_free(ser_port, 15)

    proc = subprocess.Popen([
        QEMU, "-icount", "shift=auto", "-vga", "std",
        "-drive", "format=raw,file=" + img,
        "-drive", "format=raw,file=" + disk,
        "-serial", "tcp:127.0.0.1:%d,server,nowait" % ser_port,
        "-qmp", "tcp:127.0.0.1:%d,server,nowait" % qmp_port,
    ], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        serial = SerialReader(ser_port)
        qmp = Qmp(qmp_port)
        if not wait_for(serial, "TASK: preemptive", BOOT_WAIT):
            print("FAIL: shell never came up")
            print("---- serial tail ----\n" + serial.snapshot()[-800:])
            return 1
        time.sleep(3.0)     # 与 test_sysvol 的 shell_up 一致：等提示符稳

        # exec 之后外壳仍在（ver 能答）——证明 exec 的地址空间拆换没留后患
        ok_shell, _ = run_cmd(qmp, serial, "ver", "version", 30.0)
        check(ok_shell, "shell ready before dirwalk (ver)")

        # ===== A. dirwalk 自检（内置系统卷侧） =====
        # 锚点用退出码行而不是 ALL OK：退出码在程序输出**之后**打印，
        # 等到它时 out 里必然已包含全部自检行——少一个"输出被截到一半"
        # 的侥幸窗口。
        before = serial.size()
        ok, out = run_cmd(qmp, serial, "exec dirwalk",
                          "exited with code 0", 90.0)
        raw = serial.tail_from(before)     # 未过 flat()：大小写原样
        check(ok, "exec dirwalk exits with code 0")
        if ok:
            check("userfs: all ok (0 failures)" in out,
                  "dirwalk self-check all pass")
            check("userfs: printf d=-42 i=-42 u=42 x=2a X=2A c=C s=str pct=%"
                  in raw, "printf all specifiers byte-exact (case included)")
            m = re.search(r"userfs: readdir /bin -> (\d+) entries", out)
            check(m is not None and int(m.group(1)) >= 5,
                  "readdir /bin lists the built-in programs (%s)"
                  % (m.group(1) if m else "no line"))
            check("userfs: pass hello.elf listed with size and not-dir" in out,
                  "hello.elf entry has size and is not a dir")
            check("userfs: pass readdir missing dir rejected" in out,
                  "readdir on missing dir rejected")
            check("userfs: pass readdir on a file rejected" in out,
                  "readdir on a file rejected (ENOTDIR)")
            check("userfs: pass malloc two blocks" in out
                  and "userfs: pass blocks do not overlap" in out
                  and "userfs: pass freed block reused in place (coalesce)"
                  in out
                  and "userfs: pass malloc(huge) rejected" in out
                  and "userfs: pass heap healthy after double-free rejected"
                  in out,
                  "malloc/free: no overlap, coalesce reuse, guards hold")
            check("libc: free: bad pointer or double free, ignored" in out,
                  "double-free caught and reported (not silent)")
            check("userfs: pass stat file gives size" in out,
                  "stat file gives size")
            check("userfs: pass stat /bin says directory" in out,
                  "stat /bin says directory")
            check("userfs: pass stat missing rejected" in out,
                  "stat missing path rejected")
        else:
            print("---- dirwalk output ----\n" + out[-1500:])

        # ===== B. 数据盘侧 stat =====
        ok3, out3 = run_cmd(qmp, serial, "write D1.TXT 12345678",
                            "file written successfully.", 30.0)
        check(ok3, "shell write D1.TXT (8 bytes)")
        if ok3:
            ok4, out4 = run_cmd(qmp, serial, "exec dirwalk /D1.TXT",
                                "exited with code 0", 90.0)
            check(ok4, "exec dirwalk /D1.TXT exits with code 0")
            check(ok4 and "userfs: all ok (0 failures)" in out4,
                  "dirwalk with extra path all pass")
            check(ok4 and "userfs: pass stat data-disk file gives written size"
                  in out4, "stat on data-disk file gives written size")
            check(ok4 and "userfs: stat /d1.txt -> rc=0 size=8 dir=0" in out4,
                  "stat /D1.TXT line shows rc=0 size=8 dir=0 (flat lowercases)")
        else:
            check(False, "exec dirwalk /D1.TXT exits with code 0")
            check(False, "dirwalk with extra path all pass")
            check(False, "stat on data-disk file gives written size")

        # ===== C. 清理 + 外壳最终确认 =====
        run_cmd(qmp, serial, "rm D1.TXT", ">", 20.0)
        ok5, _ = run_cmd(qmp, serial, "ls", ">", 20.0)
        check(ok5, "shell prompt returns after cleanup")

        print("== userfs: %d/%d passed =="
              % (len(RESULTS) - sum(1 for ok, _ in RESULTS if not ok),
                 len(RESULTS)))
        return 1 if any(not ok for ok, _ in RESULTS) else 0
    finally:
        try:
            qmp.quit()
        except Exception:
            pass
        try:
            serial.close()
        except Exception:
            pass
        proc.kill()
        try:
            proc.wait(timeout=15)
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
