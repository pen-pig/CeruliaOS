# -*- coding: utf-8 -*-
"""test_perm.py - P2 权限体系：/etc/passwd 用户库 + ext4 mode 位检查 + chmod/chown

验收点：
  * ext4 卷上文件有真实 mode/uid（内核 format 默认 0644 root）。
  * useradd 写 /etc/passwd（name:uid:gid），login/su/whoami 切换当前用户。
  * su 普通用户后：cat 无 r 权限的文件被拒（Permission denied），有 r 的照常；
    对 root 属主且 0755 的根目录 write 失败（父目录 w 位）。
  * chmod/chown 写回 inode（root 会话），属主变更后权限判定跟着变。
  * ls -l 显示 rwx 位与属主名。
  * 一整套 chmod/chown/useradd 写回之后 e2fsck -fn 仍干净（metadata_csum 校验
    和跟着 inode 一起更新，这是写回正确性的硬验收）。
  * FAT/exFAT 无 Unix mode：chmod 报 not supported，普通用户读写照常（全权限）。
端口由 ezos_qemu.alloc_port() 分配（每脚本唯一，支持 -j 并行）。
用法：python tests/test_perm.py   （退出码 0 = 全通过）
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
LOG = log_path(os.path.join(HERE, "perm_serial.log").replace("\\", "/"))
DISK = os.path.join(HERE, "perm_disk.img").replace("\\", "/")
DISK_BYTES = 64 * 1024 * 1024

KEYMAP = {' ': 'spc', '.': 'dot', '-': 'minus', '_': 'shift-minus',
          '/': 'slash', '*': 'kp_multiply', '+': 'kp_add'}

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


def wsl(cmd):
    cmd = cmd.replace("D:\\", "/mnt/d/").replace("D:/", "/mnt/d/")
    return subprocess.run(["wsl", "-e", "bash", "-c", cmd],
                          capture_output=True, text=True, timeout=120)


def check_ext4_perm():
    fresh_disk()
    p, qmp = launch(DISK)
    try:
        ok, out = run_cmd(qmp, "format ext4", timeout=120.0)
        rec("ext4: format ext4", ok and "formatted" in out, out[:80])
        if not ok:
            return
        run_cmd(qmp, "write A.TXT aaa")
        run_cmd(qmp, "write B.TXT bbb")

        # ---- 用户库 ----
        ok, out = run_cmd(qmp, "useradd bob")
        rec("ext4: useradd bob", ok and "user added" in out, out[:120])
        ok, out = run_cmd(qmp, "cat /etc/passwd")
        rec("ext4: /etc/passwd has bob:1:1", ok and "bob:1:1" in out, out[:140])
        ok, out = run_cmd(qmp, "whoami")
        rec("ext4: whoami is root", ok and "root" in out, out[:80])

        # ---- ls -l 属主 + 权限位 ----
        ok, out = run_cmd(qmp, "ls -l")
        rec("ext4: ls -l shows rw-r--r-- and owner root",
            ok and "rw-r--r--" in out and "root" in out and "a.txt" in out, out[:200])

        # ---- chmod 后受限用户读被拒 ----
        ok, out = run_cmd(qmp, "chmod 600 A.TXT")
        rec("ext4: chmod 600 as root", ok and "mode changed" in out, out[:120])
        ok, out = run_cmd(qmp, "su bob")
        rec("ext4: su bob", ok and "bob" in out, out[:80])
        ok, out = run_cmd(qmp, "cat A.TXT")
        rec("ext4: bob cat 600 file denied",
            ok and "permission denied" in out, out[:120])
        ok, out = run_cmd(qmp, "cat B.TXT")
        rec("ext4: bob cat 644 file ok",
            ok and "bbb" in out and "permission denied" not in out, out[:120])
        ok, out = run_cmd(qmp, "write C.TXT ccc")
        rec("ext4: bob write into root denied (dir 0755 root)",
            ok and "failed to write" in out, out[:120])

        # ---- chown 后属主权限跟着变 ----
        ok, out = run_cmd(qmp, "su root")
        ok, out = run_cmd(qmp, "chown bob A.TXT")
        rec("ext4: chown bob as root", ok and "owner changed" in out, out[:120])
        ok, out = run_cmd(qmp, "su bob")
        ok, out = run_cmd(qmp, "cat A.TXT")
        rec("ext4: bob cat own file ok after chown",
            ok and "aaa" in out and "permission denied" not in out, out[:120])
        ok, out = run_cmd(qmp, "chmod 000 A.TXT")
        rec("ext4: owner may chmod own file", ok and "mode changed" in out, out[:120])
        ok, out = run_cmd(qmp, "su root")
        ok, out = run_cmd(qmp, "chmod 644 A.TXT")
        rec("ext4: restore 644", ok and "mode changed" in out, out[:120])
        ok, out = run_cmd(qmp, "chown 5 A.TXT")
        rec("ext4: chown unknown user rejected", ok and "no such user" in out, out[:120])
    finally:
        shutdown(p)

    # ---- 硬验收：写回后卷仍然干净（metadata_csum 跟着 inode 更新） ----
    # 卷带 MBR，e2fsck 拒绝检查整盘镜像：提取分区部分再跑（同 test_ext4mkfs）
    part = os.path.join(HERE, "perm_part.img").replace("\\", "/")
    with open(part, "wb") as out_f, open(DISK, "rb") as in_f:
        in_f.seek(512)
        while True:
            chunk = in_f.read(1 << 20)
            if not chunk:
                break
            out_f.write(chunk)
    r = wsl("e2fsck -fn %s; echo RC=$?" % part)
    ok = "RC=0" in r.stdout
    rec("ext4: e2fsck -fn clean after chmod/chown/useradd writes", ok,
        r.stdout[-160:].replace("\n", " "))


def check_fat_perm():
    fresh_disk()
    p, qmp = launch(DISK)
    try:
        ok, out = run_cmd(qmp, "format exfat", timeout=120.0)
        rec("exfat: format exfat", ok and "formatted" in out, out[:80])
        if not ok:
            return
        ok, out = run_cmd(qmp, "chmod 644 A.TXT")
        rec("exfat: chmod not supported", ok and "not supported" in out, out[:120])
        ok, out = run_cmd(qmp, "useradd bob")
        ok, out = run_cmd(qmp, "su bob")
        ok, out = run_cmd(qmp, "write F.TXT fff")
        rec("exfat: bob writes fine (no unix mode)",
            ok and "successfully" in out, out[:120])
        ok, out = run_cmd(qmp, "rm F.TXT")
        rec("exfat: bob rm fine", ok and "deleted" in out, out[:120])
    finally:
        shutdown(p)


def main():
    print("PERM: unix permission model on ext4, users via /etc/passwd")
    check_ext4_perm()
    check_fat_perm()

    bad = [n for (n, ok, _) in results if not ok]
    print("---- %d/%d passed ----" % (len(results) - len(bad), len(results)))
    if bad:
        for n in bad:
            print("  FAILED: %s" % n)
        print("PERM: FAIL")
        return 1
    print("PERM: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
