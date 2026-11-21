"""test_ntp.py - SNTP 客户端 E2E（RFC 4330 简化：客户端一次查询）

判定走串口（COM1）按子串断言；协议字段走 Python 侧自建的 NTP 服务器
（`-netdev socket` 直连，完全掌控报文），与 test_dhcp 同一套框架。

要证明的三件事（缺一件就算没做成）：
  1. 请求形态对：UDP 123、48 字节、LI=0/VN=4/Mode=3（客户端）
  2. 结果真的应用了：串口打出 `NTP: unix <秒> utc <日期时间>`，
     unix 秒与服务器下发的 transmit timestamp 精确一致（减 2208988800）
  3. 反向组（服务器静默 / 应答 mode 非 4）：必须打 `NTP: timeout`，
     **绝不**出现假的 "NTP: unix"

用法：python tests/test_ntp.py   （退出码 0 = 全通过）
"""
import os as _os_ez
import sys as _sys_ez
_sys_ez.path.insert(0, _os_ez.path.dirname(_os_ez.path.abspath(__file__)))
from ezos_qemu import alloc_port, image_path, disk_path

import os
import sys
import shutil
import socket
import struct
import subprocess
import threading
import datetime
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import test_dhcp as T
from test_nvme import SerialReader, Qmp, flat, wait_for, kill_all_qemu, run_cmd

QEMU = T.QEMU
IMG = image_path()
DISK = disk_path()

PORT_A = alloc_port(3)  # (QMP, serial, netdev)
PORT_B = alloc_port(3)
PORT_C = alloc_port(3)
BOOT_WAIT = 150

WORK = {
    "A": os.path.join(HERE, "ntp_a.vhd").replace("\\", "/"),
    "B": os.path.join(HERE, "ntp_b.vhd").replace("\\", "/"),
    "C": os.path.join(HERE, "ntp_c.vhd").replace("\\", "/"),
}

SERVER_IP = T.SERVER_IP          # 10.0.2.2
HOST_MAC = T.HOST_MAC
NTP_DELTA = 2208988800           # 1900 -> 1970 秒差
TEST_UNIX = 1760000000           # 服务器下发的时间（Unix 秒）
TEST_FRAC = 0x40000000           # .25 秒（换算时应被截掉）


# ---------------- 自建 NTP 服务器（跑在 netdev socket 对端） ----------------

def parse_ntp(frame):
    """从以太帧解出 SNTP 请求字段；不是发往 123 的 48 字节 UDP 返回 None。"""
    if len(frame) < 34:
        return None
    if struct.unpack(">H", frame[12:14])[0] != 0x0800:
        return None
    ip = frame[14:]
    if len(ip) < 20 or ip[9] != 17:
        return None
    ihl = (ip[0] & 0x0F) * 4
    udp = ip[ihl:]
    sport, dport, ulen, usum = struct.unpack(">HHHH", udp[:8])
    if dport != 123 or ulen != 8 + 48 or len(udp) < 8 + 48:
        return None
    if usum != 0 and usum != T.udp_checksum(ip[12:16], ip[16:20], udp[:ulen]):
        return None
    d = udp[8:]
    return {
        "li_vn_mode": d[0],
        "vn": (d[0] >> 3) & 7,
        "mode": d[0] & 7,
        "ts": struct.unpack(">II", d[40:48]),
        "src_ip": bytes(ip[12:16]),
        "src_mac": bytes(frame[6:12]),
        "src_port": sport,
    }


def build_reply(req, unix, mode=4):
    """构造服务器应答：以太帧，IP 源 SERVER_IP 目的请求方，UDP 123 -> 客户端口。"""
    d = bytearray(48)
    d[0] = 0x20 | (4 << 3) | mode   # LI=0 VN=4 Mode=4(server)
    d[1] = 1                         # stratum
    d[2] = 6                         # poll
    d[3] = 0xFA                      # precision
    d[40:48] = struct.pack(">II", unix + NTP_DELTA, TEST_FRAC)
    udp = struct.pack(">HHHH", 123, req["src_port"], 8 + 48, 0) + bytes(d)
    udp = udp[:6] + struct.pack(
        ">H", T.udp_checksum(SERVER_IP, req["src_ip"], udp)) + udp[8:]
    total = 20 + len(udp)
    ip = struct.pack(">BBHHHBBH", 0x45, 0, total, 0x4001, 0, 64, 17, 0) + \
        SERVER_IP + req["src_ip"]
    ip = ip[:10] + struct.pack(">H", T.cksum(ip)) + ip[12:]
    return req["src_mac"] + HOST_MAC + b"\x08\x00" + ip + udp


class NtpServer(threading.Thread):
    """mode: 'ok' 正常应答 / 'silent' 收下不回 / 'badmode' 回 mode=5"""

    def __init__(self, port, mode):
        threading.Thread.__init__(self)
        self.daemon = True
        self.port = port
        self.mode = mode
        self.running = True
        self.lock = threading.Lock()
        self.reqs = []
        self.buf = b""
        self.sock = None
        self.frames = []          # 调试：所有原始帧的 (长度, ethertype)
        self.txlog = []           # 调试：发出的帧（hex 前 64B）
        self.err = None           # 调试：连接失败原因

    def stop(self):
        self.running = False
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass

    def seen(self):
        with self.lock:
            return list(self.reqs)

    def run(self):
        try:
            self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        except Exception as e:
            self.err = "connect %d: %s" % (self.port, e)
            return
        self.sock.settimeout(0.3)
        while self.running:
            frame = self._recv()
            if not frame or len(frame) < 34:
                continue
            et = struct.unpack(">H", frame[12:14])[0]
            with self.lock:
                self.frames.append((len(frame), et))
            if et == 0x0806:                          # ARP：让请求方能解析服务器
                arp = frame[14:]
                if len(arp) >= 28 and struct.unpack(">H", arp[6:8])[0] == 1 \
                        and arp[24:28] == SERVER_IP:
                    self._send(T.arp_reply_to(frame))
                continue
            req = parse_ntp(frame)
            if not req:
                continue
            with self.lock:
                self.reqs.append(req)
            if self.mode == "silent":
                continue
            self._send(build_reply(req, TEST_UNIX,
                                   mode=5 if self.mode == "badmode" else 4))

    def _send(self, frame):
        if len(frame) < 60:
            frame = frame + b"\x00" * (60 - len(frame))
        with self.lock:
            self.txlog.append(frame[:64].hex())
        try:
            self.sock.sendall(struct.pack(">I", len(frame)) + frame)
        except Exception as e:
            with self.lock:
                self.txlog.append("SENDFAIL:" + str(e))

    def _recv(self):
        while True:
            if len(self.buf) >= 4:
                n = struct.unpack(">I", self.buf[:4])[0]
                if 14 <= n <= 65535:
                    if len(self.buf) >= 4 + n:
                        frame = self.buf[4:4 + n]
                        self.buf = self.buf[4 + n:]
                        return frame
                else:
                    frame = self.buf
                    self.buf = b""
                    return frame
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            except Exception:
                return None
            if not chunk:
                return None
            self.buf += chunk


def boot(work, ports, errf=None):
    qmp_port, ser_port, net_port = ports
    if os.path.isfile(DISK):
        shutil.copyfile(DISK, work)
    kw = {}
    if errf is not None:
        kw["stderr"] = errf
    return subprocess.Popen(
        [QEMU, "-icount", "shift=auto", "-vga", "std",
         "-drive", "format=raw,file=" + IMG,
         "-drive", "format=raw,file=" + work,
         "-serial", "tcp:127.0.0.1:%d,server,wait" % ser_port,
         "-qmp", "tcp:127.0.0.1:%d,server,nowait" % qmp_port,
         "-netdev", "socket,id=n0,listen=127.0.0.1:%d" % net_port,
         "-device", "rtl8139,netdev=n0,mac=52:54:00:12:34:56"],
        cwd=ROOT, stdout=subprocess.DEVNULL, **kw)


def teardown(proc, qmp, serial, srv, qmp_port):
    try:
        if srv:
            srv.stop()
    except Exception:
        pass
    T.teardown(proc, qmp, serial, srv, qmp_port)


def expect_utc():
    return datetime.datetime.utcfromtimestamp(TEST_UNIX).strftime("%Y-%m-%d %H:%M:%S")


# ---------------- 三组用例 ----------------

def case_ok():
    """A：服务器正常应答 -> 打出精确的 unix 秒与 UTC 日期时间"""
    res = []
    proc = boot(WORK["A"], PORT_A)
    serial = qmp = srv = None
    try:
        time.sleep(1.0)
        serial = SerialReader(PORT_A[1])
        qmp = Qmp(PORT_A[0])
        srv = NtpServer(PORT_A[2], "ok")
        srv.start()
        time.sleep(0.5)
        if not T.shell_up(serial, qmp):
            print("  FAIL A: shell did not come up")
            return [("shell up", False)]

        ok, out = run_cmd(qmp, serial, "ntp 10.0.2.2", "ntp:", 40.0)
        res.append(("reported a result", ok))
        res.append(("unix seconds exact",
                    "ntp: unix %d" % TEST_UNIX in out))
        res.append(("utc date+time exact",
                    "utc %s" % expect_utc() in out))
        res.append(("fraction was dropped (no fake precision)",
                    "ntp: unix %d.25" % TEST_UNIX not in out))

        reqs = srv.seen()
        res.append(("server really got one request", len(reqs) == 1))
        if reqs:
            r = reqs[0]
            res.append(("request is 48-byte SNTP", r["ts"] != (0, 0)))
            res.append(("version = 4", r["vn"] == 4))
            res.append(("mode = 3 (client)", r["mode"] == 3))
            res.append(("client carried its RTC transmit timestamp",
                        r["ts"][0] != 0))
        return res
    finally:
        teardown(proc, qmp, serial, srv, PORT_A[0])


def case_silent():
    """B：服务器收下不回 -> 报 timeout，绝不假成功"""
    res = []
    proc = boot(WORK["B"], PORT_B)
    serial = qmp = srv = None
    try:
        time.sleep(1.0)
        serial = SerialReader(PORT_B[1])
        qmp = Qmp(PORT_B[0])
        srv = NtpServer(PORT_B[2], "silent")
        srv.start()
        time.sleep(0.5)
        if not T.shell_up(serial, qmp):
            print("  FAIL B: shell did not come up")
            return [("shell up", False)]

        ok, out = run_cmd(qmp, serial, "ntp 10.0.2.2", "ntp:", 40.0)
        res.append(("reported a result", ok))
        res.append(("reported timeout", "ntp: timeout" in out))
        res.append(("no bogus success", "ntp: unix" not in out))
        res.append(("server really got the request", len(srv.seen()) == 1))
        return res
    finally:
        teardown(proc, qmp, serial, srv, PORT_B[0])


def case_badmode():
    """C：应答 mode=5（非服务器报文）-> 不认账，报 timeout"""
    res = []
    proc = boot(WORK["C"], PORT_C)
    serial = qmp = srv = None
    try:
        time.sleep(1.0)
        serial = SerialReader(PORT_C[1])
        qmp = Qmp(PORT_C[0])
        srv = NtpServer(PORT_C[2], "badmode")
        srv.start()
        time.sleep(0.5)
        if not T.shell_up(serial, qmp):
            print("  FAIL C: shell did not come up")
            return [("shell up", False)]

        ok, out = run_cmd(qmp, serial, "ntp 10.0.2.2", "ntp:", 40.0)
        res.append(("reported a result", ok))
        res.append(("rejected non-server reply (timeout)",
                    "ntp: timeout" in out))
        res.append(("no bogus success", "ntp: unix" not in out))
        res.append(("server really replied (mode=5)", len(srv.seen()) == 1))
        return res
    finally:
        teardown(proc, qmp, serial, srv, PORT_C[0])


def main():
    print("== test_ntp: SNTP client ==")
    kill_all_qemu()
    total = 0
    failed = 0
    for name, fn in (("A ok", case_ok),
                     ("B silent", case_silent),
                     ("C badmode", case_badmode)):
        print("-- %s --" % name)
        res = fn()
        for label, ok in res:
            total += 1
            if not ok:
                failed += 1
            print("   %s %s" % ("PASS" if ok else "FAIL", label))
    print("\n%d/%d passed" % (total - failed, total))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
