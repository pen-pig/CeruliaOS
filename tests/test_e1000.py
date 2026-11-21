# -*- coding: utf-8 -*-
"""test_e1000.py - e1000 网卡驱动 E2E（复用 test_dhcp 的 harness，换 -device e1000）

要证明的三件事：
  1. 驱动真的认领了 e1000：启动横幅打出 "NIC: e1000 MAC 52:54:00:..."，
     `nic` 命令显示 MMIO base/IRQ/MAC（不是回退到 rtl8139）
  2. 帧收发真的通：DHCP 走完整 D/O/R/A 拿到 10.0.2.66，协议侧确实
     收到 DISCOVER/REQUEST 且校验和都对
  3. 换址后 ping 网关真的通：ICMP echo 到达对端，源地址是租来的

用法：python tests/test_e1000.py   （退出码 0 = 全通过）
"""
import os as _os_ez
import sys as _sys_ez
_sys_ez.path.insert(0, _os_ez.path.dirname(_os_ez.path.abspath(__file__)))

import os
import shutil
import sys
import time

from ezos_qemu import alloc_port, image_path, disk_path
import test_dhcp as T

QEMU = T.QEMU
IMG = image_path()
DISK = disk_path()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORK = os.path.join(HERE, "e1000_a.vhd").replace("\\", "/")

PORT_A = alloc_port(3)
GUEST_MAC = bytes([0x52, 0x54, 0x00, 0x12, 0x34, 0x56])


def boot_e1000(work, ports):
    """与 test_dhcp.boot 同款，网卡换成 e1000。"""
    qmp_port, ser_port, net_port = ports
    if os.path.isfile(DISK):
        shutil.copyfile(DISK, work)
    return T.subprocess.Popen(
        [QEMU, "-icount", "shift=auto", "-vga", "std",
         "-drive", "format=raw,file=" + IMG,
         "-drive", "format=raw,file=" + work,
         "-serial", "tcp:127.0.0.1:%d,server,wait" % ser_port,
         "-qmp", "tcp:127.0.0.1:%d,server,nowait" % qmp_port,
         "-netdev", "socket,id=n0,listen=127.0.0.1:%d" % net_port,
         "-device", "e1000,netdev=n0,mac=52:54:00:12:34:56"],
        cwd=ROOT, stdout=T.subprocess.DEVNULL, stderr=T.subprocess.DEVNULL)


def main():
    print("== test_e1000: e1000 NIC driver E2E ==")
    T.kill_all_qemu()
    res = []
    proc = boot_e1000(WORK, PORT_A)
    serial = qmp = srv = None
    try:
        time.sleep(1.0)
        serial = T.SerialReader(PORT_A[1])
        qmp = T.Qmp(PORT_A[0])
        srv = T.DhcpServer(PORT_A[2], "ok")
        srv.start()
        time.sleep(0.5)
        if not T.shell_up(serial, qmp):
            print("  FAIL: shell did not come up")
            return 1
        body = T.flat(serial.tail_from(0))

        # 1) 认领的是 e1000 而不是回退 rtl8139；MAC 与命令行一致
        res.append(("claimed the e1000 device", "nic: e1000 mac" in body))
        res.append(("MAC matches -device mac=", "mac 52:54:00:12:34:56" in body))
        res.append(("no rtl8139 fallback", "rtl8139" not in body))

        # 2) DHCP 完整走通
        ok, out = T.run_cmd(qmp, serial, "dhcp", "dhcp: got", 40.0)
        res.append(("lease acquired", ok))
        if not ok:
            print("---- serial tail ----\n" + out[-1200:])
        body = T.flat(serial.tail_from(0))
        res.append(("got offered address 10.0.2.66", "dhcp: got 10.0.2.66" in body))
        res.append(("no timeout/NAK branch taken",
                    "dhcp: no response" not in body and "dhcp: nak" not in body))
        disc = srv.seen(1)
        req = srv.seen(3)
        res.append(("DISCOVER received", len(disc) >= 1))
        res.append(("REQUEST received", len(req) >= 1))
        if disc:
            res.append(("DISCOVER checksums valid",
                        disc[0]["ip_sum_ok"] and disc[0]["udp_sum_ok"]))

        # 3) 换址后 ping 网关真的通
        ok2, out2 = T.run_cmd(qmp, serial, "ping 10.0.2.2", "reply from", 35.0)
        res.append(("ping gateway works on the new address", ok2))
        res.append(("gateway actually saw ICMP echoes", srv.pings >= 1))
        res.append(("outgoing packets really use the leased address",
                    srv.ping_src == T.OFFER_IP))
        if not ok2:
            print("---- ping tail ----\n" + out2[-800:])

        # nic 命令显示 e1000 的 MMIO base/IRQ/MAC
        ok3, out3 = T.run_cmd(qmp, serial, "nic", "mac: 52:54:00:12:34:56", 25.0)
        res.append(("`nic` shows e1000 identity", ok3))
        body = T.flat(serial.tail_from(0))
        res.append(("base is MMIO (not port io)",
                    "base 0x" in body and "0xc000" not in body))
        _report(res)
        return 0 if all(ok for _, ok in res) else 1
    finally:
        T.teardown(proc, qmp, serial, srv, PORT_A[0])


def _report(res):
    total = len(res)
    failed = sum(0 if ok else 1 for _, ok in res)
    for label, ok in res:
        print("   %s %s" % ("PASS" if ok else "FAIL", label))
    print("\n%d/%d passed" % (total - failed, total))


if __name__ == "__main__":
    sys.exit(main())
