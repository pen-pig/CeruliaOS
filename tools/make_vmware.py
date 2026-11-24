# -*- coding: utf-8 -*-
"""make_vmware.py - 产出可直接用 VMware Workstation/Player 打开的虚拟机。

为什么需要它：QEMU 的 SeaBIOS 太"干净"——PCI 拓扑、USB 控制器、ACPI 表、
中断路由都理想化，真机上出问题的地方它多半不会复现。VMware 用的是
PhoenixBIOS + 更接近真机的芯片组，是介于 QEMU 和真机之间的第二道验证。

用法：
    python tools/make_vmware.py                 # 默认 usb-image.bin -> vmware/
    python tools/make_vmware.py --image X.bin
    python tools/make_vmware.py --profile cerulia  # separate VM and data disk

产物：vmware/ezos-usb.vmdk（磁盘）+ vmware/ezos.vmx（虚拟机配置）
【重要】VMware 侧请用 **legacy BIOS** 启动（firmware = "bios"）：
  我们的混合镜像 legacy 与 UEFI 双通道都能引导，但 VMware 自带的 EFI 是
  64 位，加载不了 BOOTIA32.EFI，走 UEFI 会直接 "No bootable device"。
"""
import argparse
import os
from pathlib import Path
import tempfile
import uuid

import sys as _sys
_sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ezos_env as _ezos  # noqa: E402
import subprocess
import sys
from gen_diskimg import gen_exfat

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
QEMU_IMG = _ezos.qemu_img_exe()

VMX = """.encoding = "UTF-8"
# 【必填】virtualHW.version 决定硬件/固件兼容性。VMware 26 缺这一行会直接
# 报 "Cannot read the virtual machine configuration file"，别删。参考值：
# Workstation 17=20、17.5=21、25/26=22（可用宿主机的 vmx 对照）。
virtualHW.version = "22"
# 【必填】config.version = "8" 与上面 virtualHW 配套。缺这一行时 VMware 26
# 报 "Cannot read the virtual machine configuration file"，加上就能正常启动。
config.version = "8"
displayName = "EZOS"
guestOS = "other-32"
memsize = "256"
numvcpus = "1"
# 固定 uuid.bios，避免每次重新生成 vmx 都被当成另一台新机器（宿主机会问"是否移动或复制"）。
uuid.bios = "56 4d 0f 96 11 01 ec 21-23 2c f1 83 14 80 32 68"

# legacy BIOS。别改成 efi：VMware 的 EFI 是 64 位，BOOTIA32.EFI 加载不了。
firmware = "bios"

# IDE 而不是 SCSI/SATA：内核目前只有 ATA(IDE) 与 AHCI 驱动，IDE 最稳。
ide0:0.present = "TRUE"
ide0:0.fileName = "{disk}"
ide0:0.deviceType = "disk"
scsi0.present = "FALSE"
sata0.present = "FALSE"
floppy0.present = "FALSE"
ethernet0.present = "TRUE"
ethernet0.connectionType = "nat"
ethernet0.virtualDev = "e1000"
ethernet0.addressType = "generated"

# 串口 -> 文件：内核的 COM1 诊断全落到这里，E2E 也是按行抓的。
serial0.present = "TRUE"
serial0.fileType = "file"
serial0.fileName = "ezos-serial.log"
serial0.yieldOnMsrWrite = "TRUE"

# 想试 U 盘（USB MSC）时把下面的 usb.present 打开，并在虚拟机里接上设备
usb.present = "FALSE"

bios.bootDelay = "3000"
logging = "FALSE"
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image",
                    default=os.path.join(ROOT, "usb-image.bin"),
                    help="raw 镜像（默认 usb-image.bin，双通道混合镜像）")
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "vmware"))
    ap.add_argument("--profile", choices=("ezos", "cerulia"), default="ezos",
                    help="cerulia creates a separate, persistent exFAT data disk")
    args = ap.parse_args()

    img = os.path.abspath(args.image)
    if not os.path.isfile(img):
        print("MISSING image: %s" % img)
        return 1
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    name = args.profile
    vmx, boot, data = [out / (name + suffix) for suffix in (".vmx", "-usb.vmdk", "-data.vmdk")]

    def check_idle():
        for disk in (vmx, boot, data):
            if disk.with_name(disk.name + ".lck").exists():
                raise OSError(f"Power off the VM before rebuilding: {disk}")
        # Replacing the base while a suspend/snapshot still points to it is unsafe.
        if vmx.exists() and (list(out.glob(f"{name}*.vmss")) or
                             list(out.glob(f"{name}*.vmsn")) or
                             list(out.glob(f"{name}-usb-*.vmdk"))):
            raise OSError(f"Resume/power off or consolidate snapshots before rebuilding {vmx}")

    try:
        check_idle()
        with tempfile.TemporaryDirectory(prefix="vmware-build-", dir=out) as temp:
            stage = Path(temp)
            # Convert to a temporary file first; a failed conversion cannot truncate an existing VM.
            subprocess.run([QEMU_IMG, "convert", "-f", "raw", "-O", "vmdk", img,
                            str(stage / boot.name)], check=True)
            if name == "cerulia" and not data.exists():
                raw = stage / "data.vhd"
                gen_exfat(str(raw))
                subprocess.run([QEMU_IMG, "convert", "-f", "vpc", "-O", "vmdk",
                                str(raw), str(stage / data.name)], check=True)
            if not vmx.exists():
                cfg = VMX.format(disk=boot.name)
                if name == "cerulia":
                    uid = uuid.uuid4().hex
                    pairs = " ".join(uid[i:i+2] for i in range(0, len(uid), 2))
                    cfg = (cfg.replace('displayName = "EZOS"', 'displayName = "Cerulia"')
                           .replace("56 4d 0f 96 11 01 ec 21-23 2c f1 83 14 80 32 68",
                                    pairs[:23] + "-" + pairs[24:])
                           .replace("ezos-serial.log", "cerulia-serial.log"))
                    cfg += ('nvram = "cerulia.nvram"\nide0:1.present = "TRUE"\n'
                            f'ide0:1.fileName = "{data.name}"\nide0:1.deviceType = "disk"\n')
                (stage / vmx.name).write_text(cfg, encoding="utf-8", newline="\r\n")
            check_idle()
            if (stage / data.name).exists() and not data.exists():
                os.replace(stage / data.name, data)
            os.replace(stage / boot.name, boot)
            if not vmx.exists():
                os.replace(stage / vmx.name, vmx)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"VMware build failed: {exc}", file=sys.stderr)
        return 1

    print(f"OK {boot} ({boot.stat().st_size} bytes)")
    print(f"Open in VMware: {vmx.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
