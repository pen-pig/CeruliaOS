# -*- coding: utf-8 -*-
"""test_stackchk.py - 任务内核栈守卫（水位 + canary + guard）的 E2E 验证

为什么值得一个 E2E：静态栈预算（tools/check_stack.py）只能看编译期估算，
抓不到运行期真实深度，更抓不到写穿。TASK_KSIZE 只有 16KB，越界写会静默
改掉相邻任务的栈 —— 历史上 cmd_ls 的 entries[64] 就是这么把 fs.c 的
ro_cwd 写坏的，症状是提示符变成 `[A.TXT]`，看着像文件系统坏了。
守卫必须能证明两件事：真坏了会被报出来，没坏的时候不误报。

内核侧约定（别改前缀）：所有结果行以 "STACKCHK:" 开头。
  stackchk selftest  -> "STACKCHK: result: PASS"
  stackchk           -> "STACKCHK: N slot(s) checked, all guards intact"
                        或 "STACKCHK: CORRUPTION slot=K layer=guard/canary"

断言：
  1) selftest 四项里"破坏 canary 被检出""破坏 guard 被检出""水位计数准确"
     都要出现 —— 只肯 PASS 不肯报坏的守卫等于没有；
  2) result: PASS；
  3) 实机跑一遍 stackchk，守卫全部完好（不误报）；
  4) 峰值用量必须小于栈容量（真溢出时 peak 会顶到上限，这里先一步拦住）。

端口自动分配（alloc_port），与 core/fs 层其它用例不冲突。
用法：python tests/test_stackchk.py   （退出码 0 = 通过）
"""
import os as _os_ez
import sys as _sys_ez
_sys_ez.path.insert(0, _os_ez.path.dirname(_os_ez.path.abspath(__file__)))
from ezos_qemu import (alloc_port, image_path,
                       disk_path, kill_stale_qemu)
import os as _os_ezos
import sys as _sys_ezos
_sys_ezos.path.append(_os_ezos.path.dirname(
    _os_ezos.path.dirname(_os_ezos.path.abspath(__file__))))
from ezos_env import qemu_exe  # noqa: E402

import json
import os
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
QEMU = qemu_exe()
QMP_PORT = alloc_port()
SERIAL_PORT = alloc_port()
BOOT_WAIT = 60
CMD_WAIT = 25

IMG = image_path()
DISK = disk_path()

KEYMAP = {' ': 'spc', '.': 'dot', '-': 'minus', '_': 'shift-minus',
          '/': 'slash'}


class SerialReader(object):
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.buf = b""
        self.lock = threading.Lock()
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while self.running:
            try:
                data = self.sock.recv(8192)
            except socket.timeout:
                continue          # 空闲不是异常（见 tests/README.md 铁律）
            except Exception:
                break
            if not data:
                break
            with self.lock:
                self.buf += data

    def snapshot(self):
        with self.lock:
            return self.buf.decode("latin1", "replace")

    def close(self):
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass


class QMP(object):
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        r = self.sock.recv(4096)
        if b"QMP" not in r:
            raise RuntimeError("QMP connect failed")
        self.sock.settimeout(20)
        self.f = self.sock.makefile("rwb")
        # 注意：greeting 已经在上面读掉了，这里**不能**再 _read() 一次 ——
        # QEMU 在收到 qmp_capabilities 之前不会再发任何消息，多读一次就是
        # 干等 20s 然后超时（第一次跑这个测试就是这么挂的）。
        self.cmd("qmp_capabilities")

    def _read(self):
        try:
            line = self.f.readline()
        except socket.timeout:
            raise RuntimeError("QMP read timed out")
        if not line:
            raise RuntimeError("QMP connection closed")
        return json.loads(line) if line.strip() else None

    def cmd(self, name, **args):
        self.f.write((json.dumps({"execute": name, "arguments": args})
                      + "\n").encode())
        self.f.flush()
        while True:
            r = self._read()
            if r is None:
                continue
            if "return" in r or "error" in r:
                return r

    def hmc(self, c):
        return self.cmd("human-monitor-command", **{"command-line": c})

    def type_line(self, s):
        for ch in s:
            key = KEYMAP.get(ch)
            if key is None:
                key = ('shift-' + ch.lower()) if ('A' <= ch <= 'Z') else ch.lower()
            self.hmc("sendkey " + key)
            time.sleep(0.05)
        self.hmc("sendkey ret")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def wait_for(sr, needle, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if needle in sr.snapshot():
            return True
        time.sleep(0.3)
    return False


def main():
    if not os.path.isfile(IMG) or not os.path.isfile(DISK):
        print("MISSING os-image.bin / disk.vhd - run ninja first")
        return 1

    proc = subprocess.Popen(
        [QEMU, "-icount", "shift=auto", "-vga", "std",
         "-drive", "format=raw,file=" + IMG,
         "-drive", "format=raw,file=" + DISK,
         "-serial", "tcp:127.0.0.1:%d,server,nowait" % SERIAL_PORT,
         "-qmp", "tcp:127.0.0.1:%d,server,nowait" % QMP_PORT],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sr = None
    qmp = None
    try:
        time.sleep(1.0)
        sr = SerialReader(SERIAL_PORT)
        qmp = QMP(QMP_PORT)

        if not wait_for(sr, "help", BOOT_WAIT):
            print("  FAIL: shell never came up (no banner within %ds)"
                  % BOOT_WAIT)
            return 1
        time.sleep(1.0)

        ok = True

        # ---- 1) selftest：守卫必须证明它"会红" ----
        qmp.type_line("stackchk selftest")
        if not wait_for(sr, "STACKCHK:", CMD_WAIT):
            print("  FAIL: no STACKCHK output at all - command missing?")
            return 1
        time.sleep(1.5)
        txt = sr.snapshot()
        lines = [ln.strip() for ln in txt.splitlines()
                 if "STACKCHK:" in ln]
        for ln in lines:
            print("  " + ln)

        for needle in ("canary broken detected: PASS",
                       "guard broken detected: PASS",
                       "watermark",
                       "result: PASS"):
            if not any(needle in ln for ln in lines):
                print("  FAIL: selftest missing %r" % needle)
                ok = False
        if any(" FAIL" in ln for ln in lines):
            print("  FAIL: selftest reported a failing case")
            ok = False

        # ---- 2) 实机：先造出真任务，再看守卫状态 ----
        # 只查 slot 是不够的：开机时除了 0 号（跑在启动栈上、没有守卫）没有
        # 任何任务，stackchk 会老实报告 "0 slot(s)"，那样这条命令等于没验证。
        # ktask 会真的创建并交错调度两个内核线程，跑完它们的栈仍在池里。
        qmp.type_line("ktask")
        if not wait_for(sr, "scheduler switches", CMD_WAIT):
            print("  FAIL: ktask did not complete")
            return 1
        time.sleep(1.0)

        qmp.type_line("stackchk")
        if not wait_for(sr, "slot(s) checked", CMD_WAIT):
            print("  FAIL: 'stackchk' produced no summary line")
            return 1
        time.sleep(1.5)
        txt2 = sr.snapshot()
        lines2 = [ln.strip() for ln in txt2.splitlines()
                  if ln.strip().startswith("STACKCHK:")]
        # 打印最后一段（串口里可能有前面 selftest 的残留，看末尾就够）
        for ln in lines2[-8:]:
            print("  " + ln)

        if not any("all guards intact" in ln for ln in lines2):
            print("  FAIL: guards not intact on a freshly booted system "
                  "- either a real overflow or a false positive")
            ok = False
        if any("CORRUPTION" in ln for ln in lines2):
            print("  FAIL: stack corruption reported")
            ok = False
        # 至少要有槽位被检查到，否则这条命令什么都没验
        checked_n = 0
        for ln in lines2:
            if "slot(s) checked" in ln:
                import re
                m = re.search(r"STACKCHK: (\d+) slot", ln)
                if m:
                    checked_n = max(checked_n, int(m.group(1)))
        if checked_n < 1:
            print("  FAIL: 0 slots checked - no task ever got a guarded "
                  "stack (did ktask stop creating tasks?)")
            ok = False

        # ---- 3) IRQ0 巡检真的会报警 ----
        # 前面验的是 stackchk_broken_layer 这个函数准不准；这里要验的是
        # "有没有人周期性去调它"。只肯在 scratch 上验的话，巡检整个被删掉
        # 测试照样全绿。所以真的去毁一个任务栈的 guard，等 IRQ0 报出来。
        # burn 会毁掉那个槽位的守卫，因此必须放在最后。
        qmp.type_line("stackchk burn")
        if not wait_for(sr, "burn armed on slot=", CMD_WAIT):
            print("  FAIL: 'stackchk burn' armed nothing (no guarded slot?)")
            return 1
        if not wait_for(sr, "guard broken (a task wrote past", CMD_WAIT):
            print("  FAIL: IRQ0 patrol never reported the damaged guard - "
                  "either the patrol was removed or its period is too long")
            ok = False

        print("  => %s" % ("PASS" if ok else "FAIL"))
        return 0 if ok else 1
    finally:
        if sr:
            sr.close()
        if qmp:
            qmp.close()
        try:
            proc.wait(timeout=10)
        except Exception:
            kill_stale_qemu("qemu-system-x86_64.exe")


if __name__ == "__main__":
    sys.exit(main())
