# -*- coding: utf-8 -*-
"""test_systest.py - 系统调用恶意参数的 E2E（安全边界 fuzz）

为什么值得一条 E2E：user_range_ok 是"用户态能不能借系统调用读写内核内存"
的唯一闸门。它漏判在测试机上永远看不出来 —— 正常程序传的都是合法指针，
要等有人真的传一个内核地址进来才会暴露，而那时已经是提权漏洞了。
`systest` 把常见恶意形态（内核页 / 未映射 / 长度环绕 / 越界 fd / 非法号）
在内核态直接喂给 syscall_handler，断言**一律被拒**。

判据：
  1) 每条用例都要出现且是 PASS（少一条就是用例被删了/没注册）；
  2) result: PASS；
  3) 系统还活着：跑完之后还有 shell 提示符（真被写穿的话这里就没了）。

前缀约定：只有结果行用 "SYSTEST:"（别和命令的其它输出混）。
用法：python tests/test_systest.py   （退出码 0 = 通过）
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
CMD_WAIT = 30

IMG = image_path()
DISK = disk_path()

# 与 kernel/shell_extra.c cmd_systest 的用例表一一对应（12 条 + close fd）
N_CASES = 13

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
        # greeting 已读掉，不要再多读一次（会干等到超时）
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
            print("  FAIL: shell never came up")
            return 1
        time.sleep(1.0)

        qmp.type_line("systest")
        if not wait_for(sr, "SYSTEST: result:", CMD_WAIT):
            print("  FAIL: no SYSTEST output - command missing?")
            return 1
        time.sleep(1.5)
        txt = sr.snapshot()
        lines = [ln.strip() for ln in txt.splitlines()
                 if ln.strip().startswith("SYSTEST:")]
        # 串口里可能出现两份（回显/重放），按内容去重后看条数
        seen = []
        for ln in lines:
            if ln not in seen:
                seen.append(ln)
        for ln in seen:
            print("  " + ln)

        ok = True
        cases = [ln for ln in seen if "result:" not in ln]
        if len(cases) != N_CASES:
            print("  FAIL: expected %d cases, got %d" % (N_CASES, len(cases)))
            ok = False
        fails = [ln for ln in seen if " FAIL" in ln]
        if fails:
            print("  FAIL: %d case(s) accepted a malicious argument" % len(fails))
            ok = False
        if not any("result: PASS" in ln for ln in seen):
            print("  FAIL: no 'result: PASS'")
            ok = False

        # 系统还活着：跑完恶意参数后仍有可用的 shell
        qmp.type_line("uptime")
        if not wait_for(sr, "uptime", 15):
            print("  FAIL: shell died after the malicious syscalls")
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
