# -*- coding: utf-8 -*-
"""test_usbboot.py - U 盘混合镜像的 UEFI 通道启动验证

tools/make_usb_image.py 产出的 usb-image.bin 是为真机准备的：LBA0 是带 BPB 和
活动 EFI 分区表项的 MBR，LBA1 起是 stage2+内核，引导扇区副本与 ESP 的位置
由 make_usb_image.py 按内核上限动态算出，ESP（FAT32）里有
\\EFI\\BOOT\\BOOTIA32.EFI + kernel.bin。

曾经还有一条 legacy 用例（SeaBIOS 读 LBA0 -> MBR -> 引导扇区副本 -> INT13h
直读内核），2026-10 按既定方向移除 SeaBIOS 引导链后删掉：那条路的直读窗口
预算正是内核 496KB 上限的来源，且镜像已长到 44MB。镜像里为真机 BIOS 留的
MBR/VBR 组件仍在（见 make_usb_image.py），只是不再作为 CI 断言目标。

现在只跑 UEFI：OVMF(i386) 扫分区表 -> 找到 ESP -> \\EFI\\BOOT\\BOOTIA32.EFI ->
uefi/main.c 把 KERNEL.BIN 载入 0x1300000（KERNEL_DST）并跳转。
断言"内核真的跑起来了"（串口出现内核的启动标记），外加 EZEFI 的 kernel
size 行（证明它读的是镜像里那份内核）。

端口 4521(QMP) / 4522(serial)。
"""
import os as _os_ez
import sys as _sys_ez
_sys_ez.path.insert(0, _os_ez.path.dirname(_os_ez.path.abspath(__file__)))
from ezos_qemu import alloc_port, image_path, disk_path, log_path
import os as _os_ezos
import sys as _sys_ezos
_sys_ezos.path.append(_os_ezos.path.dirname(
    _os_ezos.path.dirname(_os_ezos.path.abspath(__file__))))
from ezos_env import qemu32_exe, ovmf_fd, qemu_img_exe  # noqa: E402

import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

QEMU32 = qemu32_exe()
OVMF = ovmf_fd()
USB_IMG = os.path.join(ROOT, "usb-image.bin")

from test_nvme import SerialReader, Qmp, wait_for, wait_port_free, kill_all_qemu  # noqa
import make_usb_image  # noqa  (reuse its checker)


def ovmf_pflash_args():
    """OVMF 固件参数：兼容单文件固件（edk2-i386-code.fd）与 CODE/VARS 分体
    （OVMF32_CODE_4M.fd）。分体式的 VARS 必须可写且用副本——与 test_usbmsc
    各用一份，避免并发跑时互相写花。"""
    args = ["-drive", "if=pflash,format=raw,readonly=on,file=" + OVMF]
    vars_guess = OVMF.replace("CODE", "VARS")
    if "CODE" in OVMF and os.path.isfile(vars_guess):
        vars_copy = os.path.join(ROOT, "temp", "usbboot_ovmf_vars.fd")
        os.makedirs(os.path.dirname(vars_copy), exist_ok=True)
        shutil.copy(vars_guess, vars_copy)
        args += ["-drive", "if=pflash,format=raw,file=" + vars_copy]
    return args


def run_case(name, exe, args, port_qmp, port_serial, markers, timeout=120):
    """启动一次 QEMU，等 markers 里每一条出现（串口），返回 (ok, 详情)。"""
    proc = subprocess.Popen(
        [exe] + args +
        # -display none：无头 CI 上不关显示 QEMU 会直接退出（test_usbmsc 同款）
        ["-display", "none",
         "-serial", "tcp:127.0.0.1:%d,server" % port_serial,
         "-qmp", "tcp:127.0.0.1:%d,server,nowait" % port_qmp],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    serial = SerialReader(port_serial)
    qmp = None
    hits = {}
    try:
        # Qmp() 构造也可能抛（握手竞态）——放 try 里，否则 finally 摸不到
        # 未绑定的 qmp，QEMU 进程就成了没人杀的孤儿，占着端口连累下一轮。
        qmp = Qmp(port_qmp)
        for m in markers:
            hits[m] = wait_for(serial, m, timeout)
    finally:
        if qmp is not None:
            try:
                qmp.quit()
            except Exception:
                pass
        serial.close()
        try:
            proc.wait(timeout=10)
        except Exception:
            kill_all_qemu()
        wait_port_free(port_qmp, 15)
    ok = all(hits.values())
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    for m in markers:
        print("    %s %r" % ("[ok]  " if hits[m] else "[miss]", m))
    return ok


def main():
    if not os.path.isfile(USB_IMG):
        print("MISSING usb-image.bin - run ninja first")
        return 2
    info = make_usb_image.check_image(USB_IMG)
    print("image: %d sectors, ESP @%d (%d sectors), kernel %d sectors"
          % (info['total'], info['esp_lba'], info['esp_sectors'],
             info['kernel_sectors']))
    results = []

    # UEFI (32-bit OVMF)：唯一受支持的引导通道（legacy SeaBIOS 用例已移除）
    results.append(run_case(
        "uefi: OVMF loads \\EFI\\BOOT\\BOOTIA32.EFI",
        QEMU32,
        ["-machine", "pc", "-m", "256"] + ovmf_pflash_args() +
        ["-drive", "format=raw,file=" + USB_IMG.replace("\\", "/"),
         "-net", "none"],
        4521, 4522,
        ["EZEFI: hello", "EZEFI:kernel size=", "TASK: preemptive"], 150))

    allok = all(results)
    print("\nUSB-BOOT:", "PASS" if allok else "FAIL")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
