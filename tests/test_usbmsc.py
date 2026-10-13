# -*- coding: utf-8 -*-
"""test_usbmsc.py - USB Mass Storage（BOT+SCSI）E2E（真机点亮 H1c）

验证三件事：
  1. 内核能认领 usb-storage（MSC 接口 + 一对 bulk 端点）并读容量
     -> "USB-MSC: unit<n> addr=<a> ep_in=0x81 ep_out=0x02 sectors=<s> blksize=512 ready"
  2. U 盘被注册成块设备 drive 12..，FS 层（exFAT）能挂载并读文件
     -> setdrive 13 / ls / cat README.TXT 走完整 BOT 读路径
  3. BOT 写路径可用
     -> write USBTEST.TXT ... / cat 回读

引导链：OVMF(IA32) -> \\EFI\\BOOT\\BOOTIA32.EFI -> KERNEL.BIN（UEFI 通道，
无 SeaBIOS 的 512KiB 直读预算——内核按文件真实大小加载，3MB 窗口封顶）。
A 用例引导盘 = uefi/esp 目录的 vvfat 盘挂 usb-storage（OVMF 的 UHCI 驱动
从它引导，内核起来后 UHCI 驱动重新枚举并把它认成 unit0）；B 用例引导盘 =
usb-image.bin 挂 IDE（OVMF 扫其 FAT32 ESP），场景里没有任何 usb-storage。

铁律「警惕弱断言」+「两组结果必须相反」：
  A（两个 usb-storage：引导盘+数据盘）：必须出现 2 个 unit ready、
     2 drive(s) registered，EZEFI 标记证明真的走了 UEFI 通道，且
     setdrive 13 后 ls/cat/write 回读全通（全部走串口，零 OCR——UEFI 引导
     后 shell 在 GOP 桌面里，720x400 文本模式的 OCR 断言必然失明）。
  B（不挂 U 盘）            ：必须打印 "no mass storage device"，且**绝不**
     出现 "registered as drive" / "unit*.ready"（fail closed）。

端口 4503(QMP) / 4504(serial)。
"""
import os as _os_ez
import sys as _sys_ez
_sys_ez.path.insert(0, _os_ez.path.dirname(_os_ez.path.abspath(__file__)))
from ezos_qemu import (alloc_port, image_path,
                       disk_path, log_path, kill_stale_qemu)
import os as _os_ezos
import sys as _sys_ezos
_sys_ezos.path.append(_os_ezos.path.dirname(
    _os_ezos.path.dirname(_os_ezos.path.abspath(__file__))))
from ezos_env import qemu_exe, qemu32_exe, ovmf_fd, qemu_img_exe  # noqa: E402

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
KERNEL_DIR = os.path.join(ROOT, "kernel")

QEMU = qemu32_exe()
FD = ovmf_fd()
QMP_PORT = alloc_port()
SERIAL_PORT = alloc_port()
BOOT_WAIT = 180        # OVMF 引导（-icount 下枚举 USB 盘）比 SeaBIOS 慢
SHELL_WAIT = 12

IMG = os.path.join(ROOT, "os-image.bin")
KERNEL_BIN = os.path.join(ROOT, "kernel.bin")
USB_IMAGE = os.path.join(ROOT, "usb-image.bin")
ESP = os.path.join(ROOT, "uefi", "esp")
ESP_KERNEL = os.path.join(ESP, "kernel.bin")
BOOT_EFI = os.path.join(ESP, "EFI", "BOOT", "BOOTIA32.EFI")
# 数据盘写测试会改盘内容——绝不拿 disk.vhd 本体开刀（test_nvme 同款纪律），
# 每次 A 用例跑完基线盘保持干净，绝不给下一次跑留污染。
DISK = os.path.join(HERE, "msc_data.vhd")
DISK_SRC = disk_path()

KEYMAP = {' ': 'spc', '.': 'dot', '-': 'minus', '_': 'shift-minus',
          '/': 'slash', ':': 'shift-semicolon'}


def ovmf_pflash_args():
    """OVMF 固件参数：兼容单文件固件（edk2-i386-code.fd）与 CODE/VARS 分体
    （OVMF32_CODE_4M.fd）。分体式的 VARS 必须可写且每次用副本，不然两个
    用例并发跑会互相写花同一个文件。"""
    args = ["-drive", "if=pflash,format=raw,readonly=on,file=" + FD]
    vars_guess = FD.replace("CODE", "VARS")
    if "CODE" in FD and os.path.isfile(vars_guess):
        vars_copy = os.path.join(ROOT, "temp", "msc_ovmf_vars.fd")
        os.makedirs(os.path.dirname(vars_copy), exist_ok=True)
        shutil.copy(vars_guess, vars_copy)
        args += ["-drive", "if=pflash,format=raw,file=" + vars_copy]
    return args


def uefi_preflight():
    """UEFI 引导的前置物料：同步 esp/kernel.bin、必要时重建 usb-image.bin。
    返回 (missing 列表)。kernel.bin 与 ESP 里的副本不同步时 EZEFI 会加载
    旧内核——直接红在"EZEFI 标记有了但行为是旧的"，极难排查，所以这里
    主动同步而不是只报错。"""
    missing = []
    if not os.path.isfile(FD):
        missing.append("OVMF 固件（EZOS_OVMF）")
    if not os.path.isfile(BOOT_EFI):
        missing.append("uefi/esp/EFI/BOOT/BOOTIA32.EFI（跑 uefi/build.sh）")
    if missing:
        return missing
    if os.path.isfile(KERNEL_BIN):
        if (not os.path.isfile(ESP_KERNEL)
                or open(KERNEL_BIN, "rb").read() != open(ESP_KERNEL, "rb").read()):
            shutil.copy(KERNEL_BIN, ESP_KERNEL)
    else:
        missing.append("kernel.bin（先 ninja）")
        return missing
    # B 用例引导盘：内核/引导盘任一比 usb-image.bin 新就重建（几秒，幂等）
    newest = max(os.path.getmtime(IMG), os.path.getmtime(ESP_KERNEL))
    if (not os.path.isfile(USB_IMAGE)
            or os.path.getmtime(USB_IMAGE) < newest):
        r = subprocess.run([sys.executable,
                            os.path.join(ROOT, "tools", "make_usb_image.py")],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            missing.append("tools/make_usb_image.py 失败: " + r.stdout[-200:])
    return missing



class SerialReader(object):
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.buf = b""
        self.lock = threading.Lock()
        self.running = True
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def _run(self):
        while self.running:
            try:
                data = self.sock.recv(8192)
            except socket.timeout:
                continue          # 铁律：空闲超时不是异常
            except Exception:
                break
            if not data:
                break
            with self.lock:
                self.buf += data

    def snapshot(self):
        with self.lock:
            return self.buf.decode("latin1", "replace")

    def wait_for(self, marker, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if marker in self.snapshot():
                return True
            time.sleep(0.5)
        return False

    def close(self):
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass


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

    def quit(self):
        try:
            self.cmd("quit")
        except Exception:
            pass


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


def kill_all_qemu():
    # 本测试跑 qemu-system-i386（UEFI 惯例，与 test_uefi3 一致）——按当前
    # 二进制名杀。老写法写死 x86_64：切 i386 后残留进程永远杀不掉，端口
    # 占用会把下一个用例直接卡死在 wait_port_free。
    kill_stale_qemu(os.path.basename(QEMU))


def launch(extra_args):
    return subprocess.Popen(
        [QEMU, "-machine", "pc", "-m", "256", "-vga", "std",
         "-display", "none",
         "-icount", "shift=auto"] + ovmf_pflash_args() + [
         "-serial", "tcp:127.0.0.1:%d,server" % SERIAL_PORT,
         "-qmp", "tcp:127.0.0.1:%d,server,nowait" % QMP_PORT] + extra_args,
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def serial_lines(snap):
    """按「包含前缀」取行（铁律：shell 提示符会与 dmesg 挤同一行，
    startswith 会漏行——test_usbkbd 的教训）"""
    return [ln.strip() for ln in snap.splitlines() if "USB-MSC:" in ln]


def run_case_a():
    """A：UEFI 引导 U 盘 + 数据 U 盘，全链路（BOT 读容量 / FS 挂载 / 读 / 写）"""
    if not os.path.isfile(DISK_SRC):
        print("MISSING %s" % DISK_SRC)
        return False
    shutil.copy(DISK_SRC, DISK)           # 写测试绝不污染基线盘

    if port_in_use(QMP_PORT) or port_in_use(SERIAL_PORT):
        kill_all_qemu()
        if not (wait_port_free(QMP_PORT, 15) and wait_port_free(SERIAL_PORT, 15)):
            print("PORT occupied - abort")
            return False

    # 引导盘 = uefi/esp 目录虚拟成 vvfat FAT 盘挂 usb-storage（OVMF 从它
    # 读 \EFI\BOOT\BOOTIA32.EFI + KERNEL.BIN）；数据盘 = disk.vhd 副本。
    # 两者都挂 UHCI：OVMF 的 UHCI 驱动先把引导盘读走，ExitBootServices 后
    # 内核自己的 UHCI 驱动重新枚举，两块盘都注册进 MSC 层。
    proc = launch([
        "-drive", "if=none,format=raw,file=fat:rw:" + ESP.replace("\\", "/") + ",id=usbos",
        "-device", "piix3-usb-uhci,id=uhci",
        "-device", "usb-storage,drive=usbos,bus=uhci.0,port=1,bootindex=0",
        "-drive", "format=raw,file=" + DISK.replace("\\", "/") + ",if=none,id=usbdata",
        "-device", "usb-storage,drive=usbdata,bus=uhci.0,port=2",
    ])

    serial = None
    qmp = None
    ok = True
    try:
        time.sleep(1.0)
        serial = SerialReader(SERIAL_PORT)
        qmp = Qmp(QMP_PORT)

        if not serial.wait_for("USB-MSC:", BOOT_WAIT):
            print("  FAIL A: no 'USB-MSC:' line in serial")
            print("---- serial tail ----\n" + serial.snapshot()[-1200:])
            return False
        time.sleep(SHELL_WAIT)

        boot = serial.snapshot()

        # ---- UEFI 通道正面证明（警惕弱断言：不是"起来了就算"，要证明
        # 走的是 OVMF/EZEFI，而不是溜回 legacy BIOS）
        for m in ("EZEFI:uefi magic", "GOP: UEFI boot"):
            if m not in boot:
                print("  FAIL A: UEFI boot marker missing: %r" % m)
                ok = False

        lines = serial_lines(boot)
        for ln in lines:
            print("  klog> " + ln)

        def have(sub):
            return any(sub in l for l in lines)

        if not have("unit0 addr=") or not have("blksize=512 ready"):
            print("  FAIL A: unit0 not claimed/ready")
            ok = False
        if not have("unit1 addr="):
            print("  FAIL A: unit1 (data stick) not claimed")
            ok = False
        if not have("2 drive(s) registered as drive 12..13"):
            print("  FAIL A: expected '2 drive(s) registered as drive 12..13'")
            ok = False

        # ---- FS 全链路（串口断言；GOP 桌面下 OCR 720x400 必然失明）----
        # test_uefi3 的教训：OVMF 引导后键盘控制器有残留字节，第一条
        # sendkey 命令会被吃掉——先发一条无害命令清空缓冲。
        qmp.type_line("ver")
        time.sleep(2.0)
        qmp.type_line("setdrive 13")
        if not serial.wait_for("FS drive set to 13", 20):
            print("  FAIL A: setdrive 13 rejected (exFAT mount failed?)")
            print("---- serial tail ----\n" + serial.snapshot()[-600:])
            ok = False
        qmp.type_line("ls")
        if not serial.wait_for("README.TXT", 15):
            print("  FAIL A: ls after setdrive 13 lacks README.TXT")
            print("---- serial tail ----\n" + serial.snapshot()[-600:])
            ok = False
        qmp.type_line("cat readme.txt")
        if not serial.wait_for("Welcome to EZOS", 15):
            print("  FAIL A: cat README.TXT lacks content (BOT read path)")
            print("---- serial tail ----\n" + serial.snapshot()[-600:])
            ok = False

        # ---- BOT 写路径 ----
        qmp.type_line("write USBTEST.TXT hello-usb-msc-e2e")
        if not serial.wait_for("hello-usb-msc-e2e", 20):
            print("  FAIL A: BOT write/read-back roundtrip failed")
            print("---- serial tail ----\n" + serial.snapshot()[-600:])
            ok = False
        if "kernel panic" in serial.snapshot():
            print("  FAIL A: kernel panic in serial")
            ok = False

        return ok
    finally:
        if qmp:
            qmp.quit()
        if serial:
            serial.close()
        try:
            proc.wait(timeout=10)
        except Exception:
            kill_all_qemu()
        wait_port_free(QMP_PORT, 15)
        wait_port_free(SERIAL_PORT, 15)


def run_case_b():
    """B：UHCI 无任何设备。必须 no mass storage device 且无成功行"""
    if port_in_use(QMP_PORT) or port_in_use(SERIAL_PORT):
        kill_all_qemu()
        if not (wait_port_free(QMP_PORT, 15) and wait_port_free(SERIAL_PORT, 15)):
            print("PORT occupied - abort")
            return False

    # 无 U 盘场景：引导盘 = usb-image.bin 挂 IDE（OVMF 扫其 FAT32 ESP，
    # 本用例不带任何 usb-storage，测的就是 MSC 层 fail closed）
    proc = launch([
        "-drive", "format=raw,file=" + USB_IMAGE.replace("\\", "/"),
        "-device", "piix3-usb-uhci,id=uhci",
    ])

    serial = None
    qmp = None
    ok = True
    try:
        time.sleep(1.0)
        serial = SerialReader(SERIAL_PORT)
        qmp = Qmp(QMP_PORT)
        if not serial.wait_for("USB-MSC:", BOOT_WAIT):
            print("  FAIL B: no 'USB-MSC:' line in serial")
            print("---- serial tail ----\n" + serial.snapshot()[-1200:])
            return False
        time.sleep(SHELL_WAIT)

        lines = serial_lines(serial.snapshot())
        for ln in lines:
            print("  klog> " + ln)

        if not any("no mass storage device" in l for l in lines):
            print("  FAIL B: expected 'no mass storage device'")
            ok = False
        for bad in ("registered as drive", "ready"):
            if any(bad in l for l in lines):
                print("  FAIL B: device-less case reports '%s'" % bad)
                ok = False
        return ok
    finally:
        if qmp:
            qmp.quit()
        if serial:
            serial.close()
        try:
            proc.wait(timeout=10)
        except Exception:
            kill_all_qemu()
        wait_port_free(QMP_PORT, 15)
        wait_port_free(SERIAL_PORT, 15)


def main():
    kill_all_qemu()
    wait_port_free(QMP_PORT, 15)
    wait_port_free(SERIAL_PORT, 15)

    if not os.path.isfile(IMG):
        print("MISSING %s - run ninja first" % IMG)
        return 2
    missing = uefi_preflight()
    if missing:
        print("UEFI preflight failed:")
        for m in missing:
            print("  - " + m)
        return 2

    print("=== case: A-two-sticks(UEFI boot) ===")
    ok_a = run_case_a()
    print("  => %s" % ("PASS" if ok_a else "FAIL"))
    time.sleep(0.5)

    print("=== case: B-empty ===")
    ok_b = run_case_b()
    print("  => %s" % ("PASS" if ok_b else "FAIL"))

    print("\n==== SUMMARY ====")
    print("  [%s] A-two-sticks" % ("PASS" if ok_a else "FAIL"))
    print("  [%s] B-empty" % ("PASS" if ok_b else "FAIL"))
    return 0 if (ok_a and ok_b) else 1


if __name__ == "__main__":
    sys.exit(main())
