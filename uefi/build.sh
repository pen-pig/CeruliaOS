#!/usr/bin/env bash
# Build the IA32 and x64 EFI applications for the same i686 kernel.
set -eu

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="$SRC_DIR/esp/EFI/BOOT"
KERNEL=""
CUSTOM_OUT=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --embed-kernel) KERNEL="${2:?missing kernel path}"; shift 2 ;;
    --output-dir) CUSTOM_OUT="${2:?missing output directory}"; shift 2 ;;
    *) echo "usage: $0 [--embed-kernel kernel.bin --output-dir DIR]" >&2; exit 2 ;;
  esac
done
if [ -n "$KERNEL" ]; then
  if [ -z "$CUSTOM_OUT" ] || [ ! -s "$KERNEL" ]; then
    echo "Embedded-kernel builds need an existing kernel and a separate --output-dir" >&2
    exit 2
  fi
fi
if [ -n "$CUSTOM_OUT" ]; then OUT_DIR="$CUSTOM_OUT"; fi

TOOLS="${EZOS_TOOLS:-$SRC_DIR/../../tools}"
MINGW="${EZOS_EFI_MINGW_BIN:-/c/Program Files (x86)/Dev-Cpp/MinGW64/bin}"
I686_GCC="${EZOS_CC:-$TOOLS/i686-elf-tools-windows/bin/i686-elf-gcc.exe}"
X64_GCC="${EZOS_EFI_X64_CC:-$MINGW/gcc.exe}"
MINGW_LD="${EZOS_EFI_LD:-$MINGW/ld.exe}"
OBJCOPY="${EZOS_EFI_OBJCOPY:-$MINGW/objcopy.exe}"
NASM="${EZOS_NASM:-$TOOLS/NASM/nasm.exe}"

# 若 i686-elf-gcc 不在默认相对位置，尝试 PATH
if [ ! -x "$I686_GCC" ]; then
  I686_GCC="$(command -v i686-elf-gcc || true)"
fi
for tool in "$I686_GCC" "$X64_GCC" "$MINGW_LD" "$OBJCOPY" "$NASM"; do
  if [ ! -x "$tool" ]; then
    echo "ERROR: missing EFI build tool: $tool" >&2
    exit 1
  fi
done

mkdir -p "$OUT_DIR"

# i686-elf-gcc / ld 均为原生 Windows 程序，需要将路径转为 Windows 形态
WSRC="$(cygpath -w "$SRC_DIR")"
WOUT="$(cygpath -w "$OUT_DIR")"
default_out="$(cygpath -w "$SRC_DIR/esp/EFI/BOOT")"
if [ -n "$KERNEL" ] && [ "$WOUT" = "$default_out" ]; then
  echo "Use a separate directory for the signable build, not the normal ESP" >&2
  exit 2
fi

flags=(-c -ffreestanding -fno-builtin -fno-stack-protector -fno-ident
       -fno-asynchronous-unwind-tables -mno-sse -mno-mmx -O2 -Wall -Wextra)
embed_ia32=()
embed_x64=()
if [ -n "$KERNEL" ]; then
  define="-DKERNEL_FILE=\"$(cygpath -m "$KERNEL")\""
  "$NASM" -f win32 "$define" "$WSRC\\embedded_kernel.asm" -o "$WSRC\\embedded_ia32.o"
  "$NASM" -f win64 "$define" "$WSRC\\embedded_kernel.asm" -o "$WSRC\\embedded_x64.o"
  flags+=(-DEZOS_EMBED_KERNEL)
  embed_ia32+=("$WSRC\\embedded_ia32.o")
  embed_x64+=("$WSRC\\embedded_x64.o")
fi
"$I686_GCC" "${flags[@]}" -fno-pic "$WSRC\\main.c" -o "$WSRC\\main.o"
"$X64_GCC" "${flags[@]}" -mno-red-zone "$WSRC\\main.c" -o "$WSRC\\main_x64.o"
"$NASM" -f win64 "$WSRC\\handoff_x64.asm" -o "$WSRC\\handoff_x64.o"

# 2) 链接为 PE32 EFI 应用（subsystem 10 = EFI Application）
"$MINGW_LD" -mi386pe --subsystem 10 --entry EfiMain --strip-all \
  "$WSRC\\main.o" "${embed_ia32[@]}" -o "$WOUT\\BOOTIA32.EFI"

"$MINGW_LD" -mi386pep --subsystem 10 --entry EfiMain --strip-all \
  "$WSRC\\main_x64.o" "$WSRC\\handoff_x64.o" "${embed_x64[@]}" -o "$WOUT\\BOOTX64.EFI"

# Firmware rejects a .comment section below the PE image base.
"$OBJCOPY" --remove-section .comment "$WOUT\\BOOTIA32.EFI"
"$OBJCOPY" --remove-section .comment "$WOUT\\BOOTX64.EFI"

echo "BUILD OK -> $OUT_DIR/{BOOTIA32,BOOTX64}.EFI"
