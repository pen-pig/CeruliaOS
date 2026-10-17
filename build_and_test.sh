#!/usr/bin/env bash
# ============================================================
# build_and_test.sh - Cerulia Linux/WSL 构建入口
#
# 用法:
#   ./build_and_test.sh                # 构建并启动 QEMU
#   ./build_and_test.sh build-only     # 只构建，不启动 QEMU
#   ./build_and_test.sh no-run         # 同 build-only
#   ./build_and_test.sh clean          # 清理构建产物
#
# 为什么这个脚本自己不编译任何东西
# ---------------------------------
# 它以前是 find kernel -name '*.c' | sort 自己编译加链接的。那条路有三个
# 死法，而且从来没人发现——在 WSL 下跑必然失败:
#   1. 字母序链接。本内核对链接顺序敏感: 字母序能过开机自检，然后在进
#      shell 之前挂死。只有 build.ninja 里的显式顺序是验证过的。
#   2. 不编 boot/task_switch.o(提供 switch_to / task_irq_trampoline)、
#      rust/ezos_zig/ezos_zig.o、libezos_rs.a。三者都在链接列表里，所以
#      链接必然是 undefined reference——它一次都没成功产出过内核。
#   3. CFLAGS 与 build.ninja 不一致(缺 -Werror 和三个 -fno-align-*)，
#      即便编出来也不是同一份字节。
# 和 build_and_test.bat 一样，现在只委托 ninja: 链接顺序和编译选项的
# 唯一来源就是 build.ninja，不再存在第二条会各自漂移的构建链。
#
# 依赖: python3、ninja
# 可选: EZOS_TOOLS=<工具链目录>(会加入 PATH 供工具探测)、EZOS_PYTHON、
#       EZOS_NINJA、EZOS_JOBS
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-run}"
PYTHON="${EZOS_PYTHON:-python3}"

# 若指定 EZOS_TOOLS 目录，则加入 PATH 供 gen_ninja.py 探测工具链
if [[ -n "${EZOS_TOOLS:-}" ]]; then
    export PATH="$EZOS_TOOLS:$EZOS_TOOLS/bin:$PATH"
fi

command -v "$PYTHON" >/dev/null 2>&1 || {
    echo "[错误] 未找到 $PYTHON，请安装或用 EZOS_PYTHON 指定" >&2
    exit 1
}

NINJA="${EZOS_NINJA:-ninja}"
command -v "$NINJA" >/dev/null 2>&1 || {
    echo "[错误] 未找到 ninja，请安装或用 EZOS_NINJA 指定" >&2
    exit 1
}

if [[ "$MODE" == "clean" ]]; then
    echo "正在清理构建产物..."
    rm -f boot/boot.bin boot/stage2.bin boot/kernel_entry.o boot/task_switch.o \
          kernel/*.o kernel_raw.bin kernel.bin os-image.bin
    echo "清理完成！(build_auto.ninja 是生成的，不清理)"
    exit 0
fi

# build_auto.ninja 由 gen_ninja.py 从 build.ninja 生成: 工具链路径是探测
# 出来的，不写死盘符和用户名，换机器不用改文件。
"$PYTHON" tools/gen_ninja.py --src build.ninja --out build_auto.ninja

JOBS="${EZOS_JOBS:-$(nproc 2>/dev/null || echo 4)}"

if [[ "$MODE" == "build-only" || "$MODE" == "no-run" ]]; then
    echo "[build-only] ninja -j$JOBS building os-image.bin..."
    "$NINJA" -f build_auto.ninja -j"$JOBS" os-image.bin
    echo "[build-only] 构建完成: os-image.bin"
    exit 0
fi

echo "[run] ninja -j$JOBS 构建并启动 QEMU..."
"$NINJA" -f build_auto.ninja -j"$JOBS" run
