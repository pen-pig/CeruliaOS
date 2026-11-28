#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_usb_boot.py —— 把 usb-image.bin 安全写进 U 盘的小工具

为什么默认镜像从 os-image.bin 换成了 usb-image.bin：os-image.bin 的 LBA0 是
裸引导扇区，**既没有 BPB 也没有分区表**，不少真机 BIOS 直接拒绝它，UEFI 固件
更是根本看不见（它只扫分区里的 \\EFI\\BOOT\\BOOT*.EFI）。usb-image.bin 由
tools/make_usb_image.py 产出，是 legacy + UEFI 双通道的混合镜像，同一份镜像
两种固件都能起。要用旧的裸镜像就显式 --image ../os-image.bin。

设计原则（看 docs/USB_BOOT.md 了解为什么这样写）：
  * 默认 dry-run：只列出本机磁盘 + 校验镜像，绝不写任何东西。
  * 只有显式 --write --disk <id> 才写盘。
  * 写之前会"二次输入盘号确认"：必须手打一遍目标盘 id，输错立即中止。
  * 绝不自动选盘：目标盘必须由你用 --disk 指定。
  * 写前校验：镜像大小必须是 512 的倍数，且首扇区以 0x55 0xAA 结尾。
  * 写前打印：将要写入的字节数、目标盘容量；若镜像比盘大直接拒绝。

用法：
  python3 tools/make_usb_boot.py                 # 只读：列出磁盘 + 校验镜像
  python3 tools/make_usb_boot.py --image X.bin   # 指定镜像（默认 ../usb-image.bin）
  python3 tools/make_usb_boot.py --write --disk 2 # 真的写盘（Windows 需管理员 / Linux macOS 需 sudo）
  python3 tools/make_usb_boot.py --write --disk 2 --verify  # 写后逐块回读比对（很慢，但最稳）

注意：写错盘 = 那块盘数据全清。本工具只负责"逼你确认两遍"，后果仍需你承担。
"""

import os
import sys
import json
import subprocess
import argparse

CHUNK = 1 << 20  # 1 MiB 写入块

IS_WIN = os.name == "nt"


# --------------------------------------------------------------------------
# 磁盘枚举：返回 [ {id, path, model, size, extra}, ... ]
#   id    : 传给 --disk 的令牌（Windows 是整数盘号，Linux 是 sdX，macOS 是 diskN）
#   path  : 用于 os.open 写入的设备路径
#   size  : 字节为单位的盘容量
# --------------------------------------------------------------------------
def list_disks():
    if IS_WIN:
        return _list_windows()
    if sys.platform == "darwin":
        return _list_darwin()
    return _list_linux()


def _run(cmd, shell=False):
    """跑一条命令并返回 (stdout, rc)。

    Windows 上 PowerShell 的输出是控制台代码页（中文系统 = GBK/CP936），
    用 text=True 会默认按 UTF-8 解码，遇到非 ASCII 字节直接抛
    UnicodeDecodeError（在 subprocess 的 reader 线程里炸，掩盖真实错误）。
    这里改成按 bytes 收，再用 utf-8→gbk→latin1 依次尝试解码，
    最后 fallback 到 replace，保证不因编码问题丢输出。
    """
    try:
        out = subprocess.run(cmd, shell=shell, capture_output=True,
                             timeout=30, creationflags=(subprocess.CREATE_NO_WINDOW if IS_WIN else 0))
        return _decode(out.stdout), out.returncode
    except Exception as e:  # noqa
        return "", 1


def _decode(raw):
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _list_windows():
    disks = []
    # 优先 PowerShell Get-Disk（能看到盘号/型号/容量/总线/序列号）
    ps = ('Get-Disk | Select-Object Number,FriendlyName,Size,BusType,SerialNumber '
          '| ConvertTo-Json -Compress')
    stdout, rc = _run(["powershell", "-NoProfile", "-Command", ps])
    if rc == 0 and stdout.strip():
        try:
            obj = json.loads(stdout)
            items = obj if isinstance(obj, list) else [obj]
            for d in items:
                num = d.get("Number")
                if num is None:
                    continue
                size = d.get("Size") or 0
                disks.append({
                    "id": str(num),
                    "path": r"\\.\PhysicalDrive" + str(num),
                    "model": (d.get("FriendlyName") or "").strip(),
                    "size": int(size),
                    "extra": "Bus=%s SN=%s" % (d.get("BusType"), d.get("SerialNumber")),
                })
            return disks
        except Exception:  # noqa
            pass
    # 回退：wmic（cmd 内置可用）
    stdout, rc = _run("wmic diskdrive list brief")
    if rc == 0:
        for line in stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            # 形如：Model  Size  DeviceID  ...
            dev = parts[-1]  # \\.\PHYSICALDRIVE2
            if not dev.upper().startswith("\\\\.\\PHYSICALDRIVE"):
                continue
            num = dev.split("PHYSICALDRIVE")[-1]
            try:
                size = int(parts[1])
            except Exception:  # noqa
                size = 0
            disks.append({
                "id": num,
                "path": dev,
                "model": parts[0],
                "size": size,
                "extra": "",
            })
    return disks


def _list_linux():
    disks = []
    stdout, rc = _run(["lsblk", "-b", "-d", "-n", "-o",
                       "NAME,SIZE,MODEL,SERIAL,TRAN,TYPE"])
    if rc == 0:
        for line in stdout.splitlines():
            parts = line.split()
            if len(parts) < 6:
                continue
            name, size, model, serial, tran, dtype = parts
            if dtype != "disk":
                continue
            disks.append({
                "id": name,
                "path": "/dev/" + name,
                "model": (model + " " + serial).strip(),
                "size": int(size),
                "extra": "TRAN=%s" % tran,
            })
    return disks


def _list_darwin():
    disks = []
    # 枚举 /dev/diskN，逐个取信息（盘数量通常很少）
    for name in sorted(os.listdir("/dev")):
        if not (name.startswith("disk") and name[4:].isdigit()):
            continue
        path = "/dev/" + name
        stdout, rc = _run(["diskutil", "info", "-plist", path])
        if rc != 0 or not stdout.strip():
            continue
        try:
            import plistlib
            info = plistlib.loads(stdout.encode("utf-8", "ignore"))
        except Exception:  # noqa
            continue
        size = info.get("TotalSize", 0) or 0
        if not size:
            continue
        disks.append({
            "id": name,
            "path": path,
            "model": info.get("MediaName", "") or info.get("DeviceName", ""),
            "size": int(size),
            "extra": "Bus=%s" % info.get("BusProtocol", ""),
        })
    return disks


# --------------------------------------------------------------------------
# 镜像校验
# --------------------------------------------------------------------------
# 写入目标：镜像本身 512 对齐即可；写盘时把总长度补到 1024 扇区（512KB）的
# **整数倍**（不足 1024 扇区时补到 1024）。
# 实测（QEMU SeaBIOS 1.16.3）：993 扇区（奇数）的镜像 U 盘引导失败
# （BIOS 认出设备但读盘报错），pad 到 1024 立即成功；真机 BIOS 实现各有
# 怪癖，对齐整 KB 是零成本保险。多写的零不影响引导。
# kernel.bin / UEFI 契约不受影响：UEFI 加载器（uefi/main.c）读的是文件真实
# 大小，不是这个 pad；BIOS 路径的扇区数由 tools/make_image.py 写进引导扇区
# 0x1FC，与这里的 pad 无关。
#
# 注意：曾经这里还有一条「镜像不得超过 1024 扇区（512KB）」的预算检查。
# 那条检查是照着**裸镜像**（os-image.bin，只有引导扇区+stage2+内核）写的，
# 但本工具的默认镜像已经换成 usb-image.bin —— 它额外带一个 34MB 的 FAT32
# ESP（ESP_LBA=16384、69632 扇区，见 tools/make_usb_image.py），整镜像
# 42MB / 86016 扇区。于是文档推荐的命令
#     python tools/make_usb_boot.py --write --disk N
# 必然被自己的校验拒掉（"内核超预算了"），而内核窗口的真实上限是
# boot/layout.inc 的 KERNEL_MAX_SECTORS（3MB），跟整个镜像的大小无关。
# 真正的容量约束只有一个：镜像不能比目标盘大（main() 里已判）。
PAD_SECTORS = 1024


def pad_target_bytes(img_size):
    """写盘时的补零目标长度：1024 扇区的整数倍，且不少于镜像本身。"""
    unit = PAD_SECTORS * 512
    if img_size <= unit:
        return unit
    return ((img_size + unit - 1) // unit) * unit


def validate_image(image_path):
    if not os.path.isfile(image_path):
        return None, "镜像文件不存在: %s" % image_path
    size = os.path.getsize(image_path)
    if size % 512 != 0:
        return None, "镜像大小 %d 不是 512 的倍数（引导扇区必须 512 对齐）" % size
    with open(image_path, "rb") as f:
        first = f.read(512)
    if len(first) < 512 or first[510] != 0x55 or first[511] != 0xAA:
        return None, "首扇区结尾不是 0x55 0xAA（不是合法引导扇区）"
    return size, None


# --------------------------------------------------------------------------
# 原始块设备 I/O（write_at / read_at 抽象）
# --------------------------------------------------------------------------
# Windows 特有坑（2026-11-24 实测，**管理员权限下同样复现**）：
#   Python 的 os.open() 底层是 MSVCRT _open()。以 O_WRONLY 打开
#   \\.\PhysicalDriveN 时，CreateFile 只申请到 GENERIC_WRITE，句柄没有读
#   权限；随后的 os.write() 被内核拒为 ERROR_INVALID_HANDLE (6)，Python
#   把它映射成 OSError errno 9 "Bad file descriptor"。
#   症状很有迷惑性：os.open 本身成功（不报权限错），错误只在第一次
#   write 时才炸出来，看起来像"打开了却写不了"；提权无效。
#   → 修法：Windows 上直接用 Win32 CreateFileW，申请
#     GENERIC_READ|GENERIC_WRITE，并显式给
#     FILE_SHARE_READ|FILE_SHARE_WRITE（否则盘上有活动卷时独占打开
#     会失败，errno 32 sharing violation）。同一个句柄还能回读校验，
#     省掉第二次打开。
#   非 Windows：os.pwrite/os.pread，语义与原实现一致。
class _WinRawDevice(object):
    """Windows 原始块设备句柄（CreateFileW + WriteFile/ReadFile）。"""

    def __init__(self, path):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x00000080
        FILE_FLAG_WRITE_THROUGH = 0x80000000
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.WriteFile.restype = wintypes.BOOL
        k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.ReadFile.restype = wintypes.BOOL
        k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                         ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
        k32.SetFilePointerEx.restype = wintypes.BOOL
        k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        k32.FlushFileBuffers.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        self._k32 = k32

        h = k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE,
                             FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                             OPEN_EXISTING,
                             FILE_ATTRIBUTE_NORMAL | FILE_FLAG_WRITE_THROUGH, None)
        if h is None or h == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            raise OSError(err, "Win32 CreateFileW(%s) 失败，错误码 %d: %s"
                          % (path, err, ctypes.FormatError(err)))
        self._h = h

    def _seek(self, offset):
        if not self._k32.SetFilePointerEx(self._h, self._ctypes.c_longlong(offset),
                                          None, 0):
            err = self._ctypes.get_last_error()
            raise OSError(err, "SetFilePointerEx 失败: %s" % self._ctypes.FormatError(err))

    def write_at(self, offset, data):
        ctypes = self._ctypes
        n = ctypes.c_ulong(0)
        buf = ctypes.create_string_buffer(data, len(data))
        self._seek(offset)
        if not self._k32.WriteFile(self._h, buf, len(data), ctypes.byref(n), None):
            err = ctypes.get_last_error()
            raise OSError(err, "WriteFile(off=%d,len=%d) 失败: %s"
                          % (offset, len(data), ctypes.FormatError(err)))
        if n.value != len(data):
            raise OSError(5, "WriteFile 短写：%d/%d 字节" % (n.value, len(data)))

    def read_at(self, offset, size):
        ctypes = self._ctypes
        n = ctypes.c_ulong(0)
        buf = ctypes.create_string_buffer(size)
        self._seek(offset)
        if not self._k32.ReadFile(self._h, buf, size, ctypes.byref(n), None):
            err = ctypes.get_last_error()
            raise OSError(err, "ReadFile(off=%d,len=%d) 失败: %s"
                          % (offset, size, ctypes.FormatError(err)))
        return buf.raw[:n.value]

    def flush(self):
        if not self._k32.FlushFileBuffers(self._h):
            err = self._ctypes.get_last_error()
            raise OSError(err, "FlushFileBuffers 失败: %s" % self._ctypes.FormatError(err))

    def close(self):
        if getattr(self, "_h", None):
            self._k32.CloseHandle(self._h)
            self._h = None


class _RawDevice(object):
    """跨平台原始块设备：Windows 走 Win32 API，其他走 os.pwrite/os.pread。"""

    def __init__(self, path):
        self.path = path
        self._win = None
        self._fd = None
        if IS_WIN:
            self._win = _WinRawDevice(path)
        else:
            self._fd = os.open(path, os.O_RDWR)

    def write_at(self, offset, data):
        if self._win:
            return self._win.write_at(offset, data)
        return os.pwrite(self._fd, data, offset)

    def read_at(self, offset, size):
        if self._win:
            return self._win.read_at(offset, size)
        return os.pread(self._fd, size, offset)

    def flush(self):
        if self._win:
            self._win.flush()
        else:
            os.fsync(self._fd)

    def close(self):
        if self._win:
            self._win.close()
        elif self._fd is not None:
            os.close(self._fd)
            self._fd = None


class _WinVolumeGuard(object):
    """Windows：锁定并卸载目标磁盘上的卷。

    为什么必须做（2026-11-24 实测踩坑）：
      NTFS 挂载后会持有物理设备的写锁。对 `\\\\.\\PhysicalDriveN` 的原始写入，
      **只要落在卷覆盖的扇区范围内**，就会被内核拒绝，返回
      ERROR_ACCESS_DENIED（Python errno 5）。
      实测症状极具迷惑性：写 LBA0（不在任何卷内）成功，紧接着写
      卷首扇区就被拒 —— 看起来像"写完 1MB 就不让写了"。
      （本机 NTFS 分区偏移恰好是 1048576 = 1 MiB，与 1 MiB 写块对齐，
        所以失败点正好落在卷边界上，非常容易误判成块大小问题。）

      做法（微软文档指定的序列，与 Rufus / dd for Windows 一致）：
        1. 找到目标盘上的卷（优先用 PowerShell Get-Volume 的 UniqueId，
           它直接给 `\\\\?\\Volume{GUID}\\`；退路是 FindFirstVolumeW 枚举
           + IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS 查所属物理盘）；
        2. 对每个卷：FSCTL_LOCK_VOLUME（先加独占锁，这样 dismount 才干净）
           → FSCTL_DISMOUNT_VOLUME（卸载文件系统，让 NTFS 刷完缓存
           元数据并交出该区域）；
        3. **保持句柄打开直到写盘完成** —— 卷锁随句柄关闭才释放。

      为什么不用 PowerShell 的 Dismount-Volume：
      本机 Storage 模块**根本没有这个 cmdlet**（2026-11-24 核实：模块导出
      列表里只有 Dismount-DiskImage）。而旧代码用 `; "DONE"` 判成功，
      cmdlet 不存在时 PowerShell 报错但 "DONE" 照样输出 → **假阳性**，
      卷从未被卸载，还顺手把真正的错误吞掉了。
    """

    FSCTL_LOCK_VOLUME = 0x00090018
    FSCTL_DISMOUNT_VOLUME = 0x00090020
    FSCTL_UNLOCK_VOLUME = 0x00090028
    IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x56000000

    def __init__(self, disk_number):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self.disk_number = int(disk_number)
        self._handles = []      # 保持打开 = 保持卷锁
        self.report = []        # 给用户看的诊断行
        self.locked = False

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.FindFirstVolumeW.argtypes = [wintypes.LPWSTR, wintypes.DWORD]
        k32.FindFirstVolumeW.restype = wintypes.HANDLE
        k32.FindNextVolumeW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD]
        k32.FindNextVolumeW.restype = wintypes.BOOL
        k32.FindVolumeClose.argtypes = [wintypes.HANDLE]
        k32.FindVolumeClose.restype = wintypes.BOOL
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                        ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.DeviceIoControl.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        self._k32 = k32
        self._INVALID = ctypes.c_void_p(-1).value

    def _ioctl(self, handle, code, out_buf=None, out_len=0):
        ctypes = self._ctypes
        n = ctypes.c_ulong(0)
        ok = self._k32.DeviceIoControl(handle, code, None, 0, out_buf, out_len,
                                       ctypes.byref(n), None)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return n.value

    def _volumes(self):
        """枚举所有卷的设备路径（\\\\?\\Volume{...}\\）。"""
        ctypes = self._ctypes
        buf = ctypes.create_unicode_buffer(1024)
        h = self._k32.FindFirstVolumeW(buf, 1024)
        if h == self._INVALID or h is None:
            return []
        out = []
        try:
            while True:
                out.append(buf.value)
                if not self._k32.FindNextVolumeW(h, buf, 1024):
                    break
        finally:
            self._k32.FindVolumeClose(h)
        return out

    def _disk_of_volume(self, path):
        """问内核这个卷挂在哪个物理盘上；查不到返回 None。

        注意：卷设备路径 `\\\\?\\Volume{GUID}\\` 带尾斜杠时，用默认 flag
        打开会返回 ERROR_PATH_NOT_FOUND(3) —— 必须去掉尾斜杠，或加
        FILE_FLAG_BACKUP_SEMANTICS（当目录打开）。
        """
        ctypes = self._ctypes
        wintypes = self._wintypes

        class DISK_EXTENT(ctypes.Structure):
            _fields_ = [("DiskNumber", wintypes.DWORD),
                        ("StartingOffset", ctypes.c_longlong),
                        ("ExtentLength", ctypes.c_longlong)]

        class VOLUME_DISK_EXTENTS(ctypes.Structure):
            _fields_ = [("NumberOfDiskExtents", wintypes.DWORD),
                        ("DiskExtents", DISK_EXTENT * 32)]

        h = self._open_volume(path)
        if h is None:
            return None
        try:
            ext = VOLUME_DISK_EXTENTS()
            self._ioctl(h, self.IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS,
                        ctypes.byref(ext), ctypes.sizeof(ext))
            if ext.NumberOfDiskExtents == 0:
                return None
            return int(ext.DiskExtents[0].DiskNumber)
        except OSError:
            return None
        finally:
            self._k32.CloseHandle(h)

    def _open_volume(self, path):
        """打开一个卷设备句柄。依次尝试若干路径/flag 组合，返回第一个
        成功的句柄（失败返回 None）。调用方负责 CloseHandle。

        组合顺序（实测 2026-11-24）：
          `\\\\?\\Volume{GUID}\\` + BACKUP_SEMANTICS —— 规范形式
          `\\\\?\\Volume{GUID}`（去尾斜杠）—— 无需特殊 flag
          `\\\\.\\X:` —— 有盘符时的经典形式
        """
        ctypes = self._ctypes
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

        base = path.rstrip("\\")
        attempts = [
            (path, FILE_FLAG_BACKUP_SEMANTICS),
            (base, 0),
        ]
        for p, flags in attempts:
            if not p:
                continue
            h = self._k32.CreateFileW(p, GENERIC_READ | GENERIC_WRITE,
                                      FILE_SHARE_READ | FILE_SHARE_WRITE,
                                      None, OPEN_EXISTING, flags, None)
            if h != self._INVALID and h is not None:
                return h
        return None

    def _volumes_on_disk(self):
        """列出目标盘上的所有卷。

        主路径：PowerShell `Get-Partition -DiskNumber N | Get-Volume` 的
        UniqueId 直接就是 `\\\\?\\Volume{GUID}\\`，不用猜。
        退路：枚举所有卷 + 查各自所属物理盘。
        """
        ps = ('Get-Partition -DiskNumber %d -ErrorAction SilentlyContinue '
              '| Get-Volume -ErrorAction SilentlyContinue '
              '| ForEach-Object { $_.UniqueId }' % int(self.disk_number))
        stdout, _rc = _run(["powershell", "-NoProfile", "-Command", ps])
        found = []
        for line in (stdout or "").splitlines():
            line = line.strip()
            if line.startswith("\\\\?\\Volume{") and line not in found:
                found.append(line)
        if found:
            return found, "PowerShell Get-Volume"
        # 退路
        mine = []
        for vol in self._volumes():
            if self._disk_of_volume(vol) == self.disk_number:
                mine.append(vol)
        return mine, "FindFirstVolumeW + IOCTL extents"

    def engage(self):
        """锁定并卸载目标盘上的全部卷。返回 (是否至少锁住一个, 诊断报告)。"""
        ctypes = self._ctypes
        mine, how = self._volumes_on_disk()
        self.report.append("  目标盘上的卷：%d 个（来源：%s）" % (len(mine), how))

        for vol in mine:
            h = self._open_volume(vol)
            if h is None:
                err = ctypes.get_last_error()
                self.report.append("  %s 打开失败（错误 %d）" % (vol, err))
                continue
            try:
                # 先锁后卸载：微软文档的顺序，锁住后 dismount 才干净
                self._ioctl(h, self.FSCTL_LOCK_VOLUME)
                self.report.append("  %s 已加卷锁 (FSCTL_LOCK_VOLUME)" % vol)
                try:
                    self._ioctl(h, self.FSCTL_DISMOUNT_VOLUME)
                    self.report.append("  %s 已卸载 (FSCTL_DISMOUNT_VOLUME)" % vol)
                except OSError as e:
                    self.report.append("  %s 卸载失败（错误 %d），仅靠卷锁继续"
                                       % (vol, getattr(e, "winerror", 0) or e.errno))
                self._handles.append(h)
                self.locked = True
            except OSError as e:
                err = getattr(e, "winerror", 0) or e.errno
                self.report.append("  %s 加锁失败（错误 %d: %s）" % (vol, err, e))
                self._k32.CloseHandle(h)

        if not self.locked:
            self.report.append("  ⚠ 没能锁住任何卷 —— 写入可能在卷覆盖区被拒")
        return self.locked, self.report

    def close(self):
        """关闭句柄 = 释放卷锁。"""
        for h in self._handles:
            try:
                self._k32.CloseHandle(h)
            except Exception:  # noqa
                pass
        self._handles = []



# --------------------------------------------------------------------------
# 写入（1 MiB 块；末尾自动补零到 PAD_SECTORS 扇区）
# --------------------------------------------------------------------------
def _is_access_denied(exc):
    """判断异常是不是"设备被拒访问"（Win32 ERROR_ACCESS_DENIED / errno 5）。"""
    return getattr(exc, "winerror", 0) == 5 or exc.errno == 5


def _clear_disk(disk_number):
    """Windows：清掉目标盘的分区表（diskpart clean 的 PowerShell 等价物）。

    这是写整盘镜像的**兜底**：只要盘上还有分区，文件系统就可能持有
    物理设备写锁，让落在卷区的原始写被内核拒为 ACCESS_DENIED。
    清掉分区后没有任何卷，锁自然消失。

    注意：这一步同样会销毁该盘上的数据（本来就要被整盘覆盖）。
    用 Clear-Disk 而不是 Remove-Partition：前者是官方的 "clean" 等价物，
    会一并清掉卷与挂载点，不会残留半卸载的卷。
    """
    ps = ('Clear-Disk -Number %d -RemoveData -Confirm:$false -ErrorAction Stop; "DONE"'
          % int(disk_number))
    stdout, rc = _run(["powershell", "-NoProfile", "-Command", ps])
    ok = "DONE" in (stdout or "")
    print("  清空目标盘分区表：%s" % ("成功" if ok else "失败（退出码 %s）" % rc))
    return ok


def write_image(image_path, device_path, verify, disk_number=None):
    guard = None
    if IS_WIN and disk_number is not None:
        # 先锁卷+卸载卷：NTFS 持有物理设备写锁时，落进卷覆盖区的原始写
        # 会被内核拒为 ERROR_ACCESS_DENIED（errno 5）。
        print("  [准备] 锁定并卸载目标盘上的卷...")
        guard = _WinVolumeGuard(disk_number)
        guard.engage()
        for line in guard.report:
            print(line)
    img_size = os.path.getsize(image_path)
    pad_total = pad_target_bytes(img_size)

    def _do_write():
        written = 0
        try:
            dev = _RawDevice(device_path)
        except PermissionError:
            raise SystemExit(
                "无法以写方式打开 %s：权限不足或设备被占用。\n"
                "  Windows：请用管理员身份运行本脚本，并先在资源管理器里'弹出'该 U 盘。\n"
                "  Linux/macOS：请用 sudo 运行。\n"
                "  仍失败可改用图形化工具：Win32 Disk Imager / Rufus(DD 镜像模式) / dd。" % device_path)
        except OSError as e:
            raise SystemExit("无法打开设备 %s：%s\n提示：确认盘号/设备名正确、且不是系统盘。"
                             % (device_path, e))
        try:
            with open(image_path, "rb") as fsrc:
                while True:
                    buf = fsrc.read(CHUNK)
                    if not buf:
                        break
                    dev.write_at(written, buf)
                    written += len(buf)
            # 补零到 1024 扇区（见 PAD_SECTORS 注释）
            if written < pad_total:
                zero = b"\x00" * min(CHUNK, pad_total - written)
                while written < pad_total:
                    n = min(len(zero), pad_total - written)
                    dev.write_at(written, zero[:n])
                    written += n
                print("已补零到 %d 扇区（+%d 字节，奇数扇区数会让部分 BIOS 的 U 盘引导失败）"
                      % (pad_total // 512, pad_total - img_size))
            dev.flush()
        finally:
            dev.close()
        return written

    try:
        written = _do_write()
    except OSError as e:
        if not (IS_WIN and disk_number is not None and _is_access_denied(e)):
            raise
        # 兜底：卷锁没松开 → 删分区（锁随卷消失），从 0 重写。
        print("  ⚠ 写入被拒（错误 5 = 卷仍持有设备写锁）。")
        print("  [兜底] 清空目标盘分区表后重写...")
        if guard:
            guard.close()
            guard = None
        _clear_disk(disk_number)
        written = _do_write()
    finally:
        if guard:
            # 释放卷锁（句柄关闭 = 解锁）
            guard.close()

    if verify:
        written_back = _verify_readback(image_path, device_path)
        # 回读只比对镜像本体（img_size）；pad 出去的零不在比对范围
        if written_back != img_size:
            raise SystemExit("回读校验失败：写入 %d 字节，回读 %d 字节。" % (img_size, written_back))
        print("校验通过：回读 %d 字节与镜像一致（另有 %d 字节零填充）。"
              % (written_back, written - img_size))
    return written


def _verify_readback(image_path, device_path):
    dev = _RawDevice(device_path)
    total = 0
    try:
        with open(image_path, "rb") as fsrc:
            while True:
                src = fsrc.read(CHUNK)
                if not src:
                    break
                got = dev.read_at(total, len(src))
                if len(got) != len(src) or got != src:
                    raise SystemExit("回读比对在第 %d 字节处不一致，U 盘可能损坏。" % total)
                total += len(src)
    finally:
        dev.close()
    return total


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def human(n):
    if n <= 0:
        return "0 B"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return ("%.0f %s" % (f, u)) if u == "B" else ("%.2f %s" % (f, u))
        f /= 1024


def main():
    ap = argparse.ArgumentParser(
        description="安全地把 os-image.bin 整盘写入 U 盘（默认只读检查，不写）。")
    ap.add_argument("--image", default=None, help="镜像路径（默认 ../os-image.bin）")
    ap.add_argument("--write", action="store_true", help="真正写盘（必须同时给 --disk）")
    ap.add_argument("--disk", default=None, help="目标盘 id（Windows 如 2，Linux 如 sdb，macOS 如 disk4）")
    ap.add_argument("--verify", action="store_true", help="写后逐块回读比对（慢但稳）")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    image_path = args.image or os.path.normpath(os.path.join(here, "..", "usb-image.bin"))

    print("=" * 64)
    print("EZOS U 盘制作工具（只读优先，绝不自动选盘）")
    print("=" * 64)

    # 1) 列出本机磁盘
    disks = list_disks()
    print("\n[本机磁盘]")
    if not disks:
        print("  （未能枚举到磁盘；Windows 试了 Get-Disk/wmic，Linux 需 lsblk，macOS 需 diskutil）")
    else:
        print("  %-6s %-14s %-22s %s" % ("id", "容量", "型号", "备注"))
        for d in disks:
            print("  %-6s %-14s %-22s %s" % (d["id"], human(d["size"]),
                                             (d["model"] or "?")[:22], d.get("extra", "")))
        print("  （Windows 上 Number 0 通常是系统盘，绝对不要写它）")

    # 2) 校验镜像
    print("\n[镜像校验] %s" % image_path)
    size, err = validate_image(image_path)
    if err:
        print("  ✗ " + err)
        raise SystemExit(1)
    print("  ✓ 大小 %d 字节（%d 扇区，512 对齐），首扇区 0x55 0xAA OK" % (size, size // 512))

    if not args.write:
        print("\n[dry-run] 未指定 --write，什么都没写。")
        print("  要真正写入，请先按上面 id 确认目标盘，然后：")
        print("    python3 tools/make_usb_boot.py --write --disk <id>")
        print("  脚本会再次要求你输入盘号确认。")
        return

    if not args.disk:
        raise SystemExit("\n[--write] 必须同时用 --disk <id> 指定目标盘。上面列出的 id 就是候选。")

    # 3) 写盘：定位目标盘并二次确认
    target = None
    for d in disks:
        if str(d["id"]) == str(args.disk):
            target = d
            break
    if not target:
        raise SystemExit("\n[--disk %s] 不在本机磁盘列表中，先核对上面的 id 再写。" % args.disk)

    if size > target["size"]:
        raise SystemExit("\n[中止] 镜像 %s 比目标盘 %s（%s）还大，无法写入。" %
                         (human(size), target["id"], human(target["size"])))

    print("\n[将要写入]")
    print("  目标盘 : id=%s  path=%s  容量=%s" % (target["id"], target["path"], human(target["size"])))
    print("  写入字节: %s（仅镜像大小，不会动盘上其余空间，但会覆盖 LBA0 起的等量区域）" % human(size))
    print("  ⚠ 警告：写错盘会清空该盘全部数据，且难以恢复。")

    # 二次确认：必须原样输入盘号
    try:
        confirm = input("  请输入目标盘 id「%s」以确认写入（输错或留空=中止）：" % target["id"])
    except EOFError:
        confirm = ""
    if confirm.strip() != str(target["id"]):
        raise SystemExit("\n[已中止] 确认输入 '%s' 与 '%s' 不符，未写入任何数据。" %
                         (confirm.strip(), target["id"]))

    print("\n写入中（块大小 %s）..." % human(CHUNK))
    disk_number = target["id"] if str(target["id"]).isdigit() else None
    written = write_image(image_path, target["path"], args.verify, disk_number)
    print("✓ 完成：已写入 %s 到 %s" % (human(written), target["path"]))
    print("  别在 Windows 里点'格式化'！直接安全弹出 U 盘去真机试。")


if __name__ == "__main__":
    main()
