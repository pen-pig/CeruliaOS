# -*- coding: utf-8 -*-
r"""ezos_env.py - 项目根级的路径解析（可搬迁的唯一入口）。

**为什么需要**：仓库里曾经有 40+ 处写死 `D:/MyOS/tools/...`，换个盘符/换个
机器就编不过——而且失败方式是"ninja: command not found"这种毫无线索的报错。
现在所有外部工具都从这里解析，优先级：

  1. 环境变量（EZOS_TOOLS / EZOS_QEMU / EZOS_CARGO ...）——CI 与多版本共存用
  2. **按项目自身位置推导**：<项目根>/../tools ——仓库在 X:\MyOS\src 时自动用
     X:\MyOS\tools，天然支持 D/E 多副本，不需要任何配置
  3. 扫 C-H 盘找 MyOS/tools（老布局兜底）
  4. 最后回退 D:/MyOS/tools

任何一处都找不到时，require_* 会抛出**带修复建议**的错误，而不是让构建
脚本在几百行之后报一句 command not found。
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = os.name == "nt"

# 工具的相对位置（相对 tools 根）
REL = {
    "cc": os.path.join("i686-elf-tools-windows", "bin", "i686-elf-gcc.exe"),
    "ld": os.path.join("i686-elf-tools-windows", "bin", "i686-elf-ld.exe"),
    "objcopy": os.path.join("i686-elf-tools-windows", "bin", "i686-elf-objcopy.exe"),
    "nasm": os.path.join("NASM", "nasm.exe"),
    "zig": os.path.join("zig", "zig-x86_64-windows-0.15.1", "zig.exe"),
    "qemu": os.path.join("qemu-portable-20241220", "qemu-system-x86_64.exe"),
    "qemu32": os.path.join("qemu-portable-20241220", "qemu-system-i386.exe"),
    "qemu_img": os.path.join("qemu-portable-20241220", "qemu-img.exe"),
    "ovmf": os.path.join("qemu-portable-20241220", "share",
                         "edk2-i386-code.fd"),
    "ovmf_x64": os.path.join("qemu-portable-20241220", "share",
                             "edk2-x86_64-code.fd"),
}

ENV_KEYS = {
    "cc": "EZOS_CC", "ld": "EZOS_LD", "objcopy": "EZOS_OBJCOPY",
    "nasm": "EZOS_NASM", "zig": "EZOS_ZIG", "qemu": "EZOS_QEMU",
    "qemu32": "EZOS_QEMU32", "qemu_img": "EZOS_QEMU_IMG", "ovmf": "EZOS_OVMF",
    "ovmf_x64": "EZOS_OVMF_X64",
}


def tools_root():
    """工具根目录。环境变量 > <项目根>/../tools > 扫盘 > D:/MyOS/tools"""
    env = os.environ.get("EZOS_TOOLS")
    if env:
        return env
    sibling = os.path.normpath(os.path.join(ROOT, "..", "tools"))
    if os.path.isdir(sibling):
        return sibling
    for drive in "CDEFGH":
        cand = "%s:\\MyOS\\tools" % drive
        if os.path.isdir(cand):
            return cand
    return "D:/MyOS/tools"


def tool(name, must_exist=True):
    """按名字取工具的绝对路径。"""
    env = os.environ.get(ENV_KEYS.get(name, ""))
    if env:
        return env
    path = os.path.normpath(os.path.join(tools_root(), REL[name]))
    if must_exist and not os.path.isfile(path):
        raise SystemExit(
            "EZOS 缺少工具：%s\n"
            "  期望位置：%s\n"
            "  解决办法（三选一）：\n"
            "   1) 把该工具放到上述路径\n"
            "   2) 设环境变量 %s 指向它\n"
            "   3) 设 EZOS_TOOLS 指向完整的 tools 目录"
            % (name, path, ENV_KEYS.get(name, "EZOS_<TOOL>")))
    return path


def qemu_exe():
    return tool("qemu")


def qemu32_exe():
    return tool("qemu32")


def qemu_img_exe():
    return tool("qemu_img")


_accel_cache = None


def accel_args():
    """QEMU 加速器参数：WHPX（Windows Hypervisor Platform）优先、tcg 兜底。

    GUI 探针族（无 -icount）从这里取硬件加速参数——TCG 软件模拟下单帧
    200-400ms、一轮探针 6-7 分钟；WHPX 下接近原生速度。**带 -icount 的
    测试别用**：icount 是 TCG 专属，与 WHPX 互斥。

    按 `qemu -accel help` 的实测输出决定加不加 whpx：Linux CI 的 QEMU
    不带 whpx，而无效加速器名会直接报错（不会跳过回退），不能无条件加。
    环境变量 EZOS_QEMU_ACCEL 可强制指定，冒号分隔多个（tcg=关加速）。

    注意：加速器改变 guest 侧时序的绝对值——cgap 这类**访客毫秒**闸门在
    TCG 与 WHPX 下差数倍，预算须按实际启用的加速器校准（probe_cursor.py
    从 QEMU stderr 检测 "whpx: initialized" 后分档就是这个原因）。
    """
    global _accel_cache
    if _accel_cache is not None:
        return list(_accel_cache)
    env = os.environ.get("EZOS_QEMU_ACCEL", "").strip()
    if env:
        _accel_cache = [t for a in env.split(":") if a for t in ("-accel", a)]
        return list(_accel_cache)
    try:
        out = subprocess.run([tool("qemu"), "-accel", "help"],
                             capture_output=True, text=True, timeout=15)
        has_whpx = "whpx" in (out.stdout or "")
    except (OSError, subprocess.SubprocessError):
        has_whpx = False
    _accel_cache = (["-accel", "whpx", "-accel", "tcg"] if has_whpx else [])
    return list(_accel_cache)


def ovmf_fd():
    return tool("ovmf")


def ovmf_x64_fd():
    return tool("ovmf_x64")


def cargo_exe():
    """cargo：环境变量 > PATH > ~/.cargo/bin。

    rustup 装的 cargo 默认不进 PATH，但一定在用户目录下——写死
    C:/Users/<用户名>/.cargo 在别人机器上必然失效。
    """
    env = os.environ.get("EZOS_CARGO")
    if env:
        return env
    found = shutil.which("cargo")
    if found:
        return found
    guess = os.path.join(os.path.expanduser("~"), ".cargo", "bin",
                         "cargo.exe" if IS_WINDOWS else "cargo")
    if os.path.isfile(guess):
        return guess
    raise SystemExit(
        "EZOS 找不到 cargo（Rust 增量模块 libezos_rs.a 是链接必需项）。\n"
        "  解决办法：安装 Rust（https://rustup.rs/）后设 EZOS_CARGO 指向 cargo，\n"
        "  或把它加进 PATH。\n"
        "  若只想跳过 Rust/Zig 增量：设 EZOS_SKIP_RUSTZIG=1（会退回纯 C 实现）。")


def host_linker_dir():
    r"""Windows 上 Rust **宿主目标**的链接器目录（MSVC 的 link.exe）。

    为什么需要它：C 侧是 i686-elf 裸机工具链，跟 MSVC 无关；但 Rust 的
    build-std / proc-macro / 宿主探针是**为 Windows 宿主编译**的，需要一个
    能链接 PE 的链接器。缺它时 cargo 会报
    `linker `link.exe` not found` 或更隐蔽的
    `link: extra operand ... Try 'link --help'`——后者其实是 Git Bash 的
    /usr/bin/link.exe（GNU coreutils）**抢在前面**被调到了，不是 MSVC 出错。

    探测顺序：环境变量 EZOS_LINKER_DIR > PATH > vswhere > 自定义安装目录。
    vswhere 查询 link.exe 文件；安装记录缺失时检查项目盘的 Visual Studio*
    与 VSBuildTools* 目录。找不到返回 None（调用方自行决定是否致命）。
    """
    env = os.environ.get("EZOS_LINKER_DIR")
    if env and os.path.isdir(env):
        return env
    if not IS_WINDOWS:
        return None

    # Git Bash 里 /usr/bin/link.exe 是 coreutils 的那个同名程序，会伪装成
    # MSVC 链接器。真 MSVC 路径必然在 Program Files 或自定义安装根下。
    which = shutil.which("link.exe")
    if which and ("Visual Studio" in which or "VSBuildTools" in which
                  or "MSVC" in which):
        return os.path.dirname(which)

    vswhere = os.path.join(
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        "Microsoft Visual Studio", "Installer", "vswhere.exe")
    if os.path.isfile(vswhere):
        try:
            out = subprocess.run(
                [vswhere, "-products", "*", "-prerelease", "-latest", "-find",
                 r"VC\Tools\MSVC\**\bin\Hostx64\x64\link.exe"],
                capture_output=True, text=True, timeout=30)
            for line in (out.stdout or "").splitlines():
                p = line.strip()
                if os.path.isfile(p):
                    return os.path.dirname(p)
        except (OSError, subprocess.SubprocessError):
            pass

    # Some custom installations are usable but absent from vswhere's registry.
    # Search only the conventional VS directory layouts, not entire drives.
    import glob
    drive = os.path.splitdrive(ROOT)[0] + os.sep
    roots = [os.environ.get("VSINSTALLDIR", "")]
    roots += glob.glob(os.path.join(drive, "Visual Studio*"))
    roots += glob.glob(os.path.join(drive, "VSBuildTools*"))
    candidates = []
    for root in roots:
        if root:
            candidates.extend(glob.glob(os.path.join(
                root, "VC", "Tools", "MSVC", "*", "bin", "Hostx64", "x64",
                "link.exe")))
    for p in sorted(candidates, reverse=True):
        if os.path.isfile(p):
            return os.path.dirname(p)
    return None


def prepend_host_linker_path():
    """把 MSVC 链接器目录插到 PATH 最前面，返回是否成功。

    必须在**调用 cargo/rustc 之前**执行：PATH 里已有 /usr/bin（Git Bash）
    时，不前置就会解析到 coreutils 的 link.exe，报
    `link: extra operand` —— 看着像链接器坏了，其实是抢错了程序。
    """
    d = host_linker_dir()
    if not d:
        return False
    cur = os.environ.get("PATH", "")
    parts = cur.split(os.pathsep)
    if d.lower() in [p.lower() for p in parts]:
        return True
    os.environ["PATH"] = d + os.pathsep + cur
    return True


def kernel_version():
    """内核版本号，从 kernel/version.h 解析（唯一事实来源）。

    别在测试里抄版本字符串：git 上早就打了 v1.1.0，而内核里还印着 0.9.0，
    tests/ 里抄的四份也全是 0.9.0——改一处漏三处，版本号永远对不上，测试
    里那份过期还会变成一次莫名其妙的假红。
    """
    import re as _re
    path = os.path.join(ROOT, "kernel", "version.h")
    try:
        txt = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    m = _re.search(r'#define\s+EZOS_VERSION\s+"([^"]+)"', txt)
    return m.group(1) if m else None


def preflight(need=("cc", "ld", "objcopy", "nasm")):
    """构建前检查，返回缺失清单（空 = 齐备）。"""
    missing = []
    for name in need:
        if not os.path.isfile(tool(name, must_exist=False)):
            missing.append(name)
    return missing


if __name__ == "__main__":
    print("项目根  :", ROOT)
    print("工具根  :", tools_root())
    for k in sorted(REL):
        p = os.path.join(tools_root(), REL[k])
        print("  %-8s %s %s" % (k, "OK " if os.path.isfile(p) else "MISS", p))
    try:
        print("cargo    :", cargo_exe())
    except SystemExit as e:
        print("cargo    : 缺失 (%s)" % str(e).splitlines()[0])
