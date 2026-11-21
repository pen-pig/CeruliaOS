/*
 * bios_thunk.h - 实模式回切 INT 10h（VBE 模式设置）
 *
 * 唯一调用方是 gfx.c 的 VBE LFB 激活路径：boot.asm 只探测不切模式，
 * QEMU 走 Bochs DISPI 寄存器，VMware/真机走这里回切实模式调 INT 10h。
 * 见 bios_thunk.asm 顶部的完整设计说明。
 */
#ifndef BIOS_THUNK_H
#define BIOS_THUNK_H

#include "types.h"

/* 低内存布局（与 bios_thunk.asm 的常量必须一致） */
#define BT_STUB_DEST   0x8000u   /* 16 位 stub 复制目标 */
#define BT_PARM_AX     0x8100u   /* word: INT 10h 的 AX 入参 */
#define BT_PARM_BX     0x8102u   /* word: BX 入参（0x4F02 的模式号|0x4000） */
#define BT_PARM_CX     0x8104u   /* word: CX 入参（0x4F01 的模式号） */
#define BT_PARM_DI     0x8106u   /* word: DI 入参（0x4F01 的缓冲偏移） */
#define BT_RES_AX      0x8108u   /* word: INT 10h 返回的 AX */
#define BT_RES_BX      0x810Au   /* word: 返回的 BX（0x4F03 当前模式） */
#define BT_BACK32      0x810Cu   /* dword: stub 回跳 32 侧的目标地址 */
#define BT_RM_IDT      0x8180u   /* 6B: 实模式 IVT 伪描述符（stub 现场填写后 lidt） */
#define BT_SCRATCH     0x8200u   /* 256B scratch：VBE mode info block 落点 */

/*
 * 实模式回切调用 INT 10h。ax/bx/cx/di 原样传入；ES 恒为 0，
 * 故 di 是 0 段内的绝对偏移（缓冲区用 BT_SCRATCH）。
 * 返回 INT 10h 的 AX（VBE 成功 = 0x004F；失败码见 VBE 规范）。
 * 回切本身失败（GDT/stub 异常）返回 -1。
 */
int bios_int10(uint16_t ax, uint16_t bx, uint16_t cx, uint16_t di);

/* 上一次 bios_int10 的 BX 返回值（VBE 0x4F03 的当前模式号在 BX） */
uint16_t bios_int10_bx(void);

/* VBE mode info block 字段偏移（块落在 BT_SCRATCH） */
#define VBE_MI_BPS   0x10u   /* word  BytesPerScanLine */
#define VBE_MI_XRES  0x12u   /* word  XResolution */
#define VBE_MI_YRES  0x14u   /* word  YResolution */
#define VBE_MI_BPP   0x19u   /* byte  BitsPerPixel */
#define VBE_MI_PHYS  0x28u   /* dword PhysBasePtr（LFB 物理地址） */

#endif /* BIOS_THUNK_H */
