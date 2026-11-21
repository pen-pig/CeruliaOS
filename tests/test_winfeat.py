# -*- coding: utf-8 -*-
"""test_winfeat.py - P3 Win 系特性：/$RECYCLE.BIN 回收站 + /registry.db kv

验收点：
  * rm 移入 /$RECYCLE.BIN（<n>.dat 内容 + <n>.meta 原路径），cat 原路径不再可见。
  * trash 列出槽号/大小/原路径；restore 按槽号还原回原路径；purge 清空。
  * restore 不存在的槽报 No such slot；还原后槽位消失（再次 trash 少一条）。
  * reg set/get/del/list：覆盖写不重复、删掉的 get 回 (not set)、非法 key 拒收。
  * 回收站/注册表全部走 fs_* 统一层，FAT/exFAT/ext4 可写卷通用（本测试用 exFAT 跑通
    语义，ext4 侧权限检查已在 test_perm 覆盖）。
用法：python tests/test_winfeat.py   （退出码 0 = 全通过）
"""
import os as _os_ez
import sys as _sys_ez
_sys_ez.path.insert(0, _os_ez.path.dirname(_os_ez.path.abspath(__file__)))
from ezos_qemu import (alloc_port, image_path, disk_path,
                       log_path, kill_stale_qemu)
import os as _os_ezos
import sys as _sys_ezos
_sys_ezos.path.append(_os_ezos.path.dirname(
    _os_ezos.path.dirname(_os_ezos.path.abspath(__file__))))
from ezos_env import qemu_exe  # noqa: E402

import json
import os
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

QEMU = qemu_exe()
PORT = alloc_port()
PROMPT = b"[/] >"
BOOT_TIMEOUT = 120.0
CMD_TIMEOUT = 90.0

IMG = image_path()
SRC = disk_path()
LOG = log_path(os.path.join(HERE, "winfeat_serial.log").replace("\\", "/"))
DISK = os.path.join(HERE, "winfeat_disk.img").replace("\\", "/")
DISK_BYTES = 64 * 1024 * 1024

KEYMAP = {' ': 'spc', '.': 'dot', '-': 'minus', '_': 'shift-minus',
          '/': 'slash', '*': 'kp_multiply', '+': 'kp_add', '!': 'shift-1'}

results = []


def rec(name, ok, note=""):
    results.append((name, ok, note))
    print("  [%s] %s%s" % ("OK" if ok else "FAIL", name,
                           ("  -- " + note) if note else ""))
    sys.stdout.flush()


class Qmp(object):
    def __init__(self, port):
        for _ in range(60):
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("QMP connect failed")
        self.sock.settimeout(20)
        self.f = self.sock.makefile("rwb")
        self._read()
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
        self.f.write((json.dumps({"execute": name, "arguments": args}) + "\n").encode())
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
            r = self.hmc("sendkey " + key)
            if "error" in r:
                raise RuntimeError("sendkey %r failed: %r" % (key, r))
            time.sleep(0.05)
        self.hmc("sendkey ret")


def read_log():
    try:
        with open(LOG, "rb") as f:
            return f.read()
    except OSError:
        return b""


def flat(b):
    return " ".join(b.decode("latin1", "replace").lower().split())


def wait_for(needle, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if needle in read_log():
            return True
        time.sleep(0.5)
    return False


def run_cmd(qmp, cmd, timeout=CMD_TIMEOUT):
    n0 = len(read_log())
    qmp.type_line(cmd)
    end = time.time() + timeout
    while time.time() < end:
        new = read_log()[n0:]
        if b"\r\n" in new and new.rstrip().endswith(b">"):
            return True, flat(new)
        time.sleep(0.5)
    return False, flat(read_log()[n0:])


def port_in_use(port):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
        s.close()
        return True
    except OSError:
        return False


def wait_port_free(port, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not port_in_use(port):
            return True
        time.sleep(0.3)
    return False


def launch(disk):
    if port_in_use(PORT):
        kill_stale_qemu("qemu-system-x86_64.exe")
        wait_port_free(PORT, 15)
    if not wait_port_free(PORT, 10):
        raise RuntimeError("PORT %d occupied" % PORT)
    try:
        os.remove(LOG)
    except OSError:
        pass
    p = subprocess.Popen([
        QEMU, "-vga", "std", "-display", "none", "-m", "128",
        "-drive", "format=raw,file=" + IMG,
        "-drive", "format=raw,file=" + disk,
        "-qmp", "tcp:127.0.0.1:%d,server,nowait" % PORT,
        "-serial", "file:" + LOG,
    ], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    qmp = Qmp(PORT)
    if not wait_for(PROMPT, BOOT_TIMEOUT):
        p.kill()
        p.wait(timeout=15)
        raise RuntimeError("boot did not reach prompt")
    return p, qmp


def shutdown(p):
    p.kill()
    try:
        p.wait(timeout=15)
    except Exception:
        pass


def fresh_disk():
    shutil.copyfile(SRC, DISK)
    if os.path.getsize(DISK) < DISK_BYTES:
        with open(DISK, "r+b") as f:
            f.truncate(DISK_BYTES)


def check_trash():
    fresh_disk()
    p, qmp = launch(DISK)
    try:
        ok, out = run_cmd(qmp, "format exfat", timeout=120.0)
        rec("exfat: format", ok and "formatted" in out, out[:80])
        if not ok:
            return

        run_cmd(qmp, "write T1.TXT hello-trash")
        ok, out = run_cmd(qmp, "rm T1.TXT")
        rec("trash: rm goes to recycle bin", ok and "file deleted" in out, out[:100])
        ok, out = run_cmd(qmp, "cat T1.TXT")
        rec("trash: original path gone",
            ok and ("no such file" in out or "not found" in out), out[:100])

        run_cmd(qmp, "write T2.TXT second-file")
        run_cmd(qmp, "rm T2.TXT")
        ok, out = run_cmd(qmp, "trash")
        rec("trash: lists both entries with original paths",
            ok and "t1.txt" in out and "t2.txt" in out and "hello-trash" not in out,
            out[:220])

        ok, out = run_cmd(qmp, "restore 1")
        rec("trash: restore slot 1", ok and "restored" in out, out[:100])
        ok, out = run_cmd(qmp, "cat T1.TXT")
        rec("trash: restored content intact",
            ok and "hello-trash" in out, out[:100])
        ok, out = run_cmd(qmp, "trash")
        rec("trash: slot 1 consumed after restore",
            ok and "t1.txt" not in out and "t2.txt" in out, out[:180])

        ok, out = run_cmd(qmp, "restore 999")
        rec("trash: unknown slot rejected", ok and "no such slot" in out, out[:100])

        ok, out = run_cmd(qmp, "purge")
        rec("trash: purge reports count", ok and "purged 1" in out, out[:100])
        ok, out = run_cmd(qmp, "trash")
        rec("trash: empty after purge", ok and "empty" in out, out[:100])

        # 重复 rm 同名文件：槽位递增、不覆盖旧槽
        run_cmd(qmp, "write DUP.TXT one")
        run_cmd(qmp, "rm DUP.TXT")
        run_cmd(qmp, "write DUP.TXT two")
        run_cmd(qmp, "rm DUP.TXT")
        ok, out = run_cmd(qmp, "trash")
        rec("trash: repeated rm takes two slots", ok and out.count("dup.txt") == 2,
            out[:180])
        run_cmd(qmp, "purge")
    finally:
        shutdown(p)


def check_reg():
    fresh_disk()
    p, qmp = launch(DISK)
    try:
        ok, out = run_cmd(qmp, "format exfat", timeout=120.0)
        rec("reg: format", ok and "formatted" in out, out[:80])
        if not ok:
            return

        ok, out = run_cmd(qmp, "reg list")
        rec("reg: empty at start", ok and "empty" in out, out[:100])

        ok, out = run_cmd(qmp, "reg set host.name ezos")
        rec("reg: set ok", ok and "ok" in out, out[:100])
        ok, out = run_cmd(qmp, "reg get host.name")
        rec("reg: get roundtrip", ok and "ezos" in out and "not set" not in out,
            out[:100])

        run_cmd(qmp, "reg set host.os cerulia")
        ok, out = run_cmd(qmp, "reg list")
        rec("reg: list shows both keys",
            ok and "host.name=ezos" in out and "host.os" in out, out[:160])

        run_cmd(qmp, "reg set host.name v2")
        ok, out = run_cmd(qmp, "reg list")
        rec("reg: set overwrites without duplicate lines",
            ok and "host.name=v2" in out and out.count("host.name") == 1, out[:160])

        ok, out = run_cmd(qmp, "reg del host.os")
        rec("reg: del ok", ok and "ok" in out, out[:100])
        ok, out = run_cmd(qmp, "reg get host.os")
        rec("reg: deleted key reads not set", ok and "not set" in out, out[:100])

        ok, out = run_cmd(qmp, "reg set bad!key value")
        rec("reg: invalid key rejected", ok and "failed" in out, out[:100])
        ok, out = run_cmd(qmp, "reg get nosuch.key")
        rec("reg: unknown key reads not set", ok and "not set" in out, out[:100])
        ok, out = run_cmd(qmp, "reg get")
        rec("reg: usage on missing key", ok and "usage" in out, out[:120])
    finally:
        shutdown(p)


def main():
    print("WINFEAT: recycle bin + registry kv")
    check_trash()
    check_reg()
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("---- %d/%d passed ----" % (passed, total))
    if passed != total:
        print("  FAILED: " + "; ".join(n for n, ok, _ in results if not ok))
    print("WINFEAT: %s" % ("PASS" if passed == total else "FAIL"))
    for f in (LOG, DISK):
        try:
            os.remove(f)
        except OSError:
            pass
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
