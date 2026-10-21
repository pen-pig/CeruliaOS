/*
 * bios_thunk.c - 实模式回切的 C 侧：stub 搬运 + trip GDT + 参数传递
 *
 * 汇编侧（bios_thunk.asm）只做寄存器和控制流的搬运，所有"可变"部分
 * 都在这里落地：trip GDT 的描述符、stub 到 0x8000 的复制、INT 10h
 * 的四个入参和两个返回值、stub 回跳 32 侧的地址。
 *
 * 为什么 stub 要复制到固定低地址：16 位代码段的 IP 只有 16 位，
 * 相对段基址最多覆盖 64KB；内核在 19MB，直接跳过去编码不进 16 位
 * 远指针。复制到 0x8000 后选择子取 0x8000>>4 = 0x800，实模式远跳
 * 的 CS 基址与 16 位保护模式段基址天然一致，指令流无需重定位。
 * stub 内部因此只写 0x8xxx 绝对地址，不引用任何链接期符号。
 */
#include "bios_thunk.h"
#include "mem.h"
#include "dmesg.h"
#include "port.h"
#include "mouse.h"

extern char bios_stub16_begin[];  /* bios_thunk.asm */
extern char bios_stub16_end[];
extern void bios_thunk_enter(void);
extern uint32_t bios_back32;      /* 代码标签，取其地址用 */

/* trip GDT：257 项（选择子 0x800 = 第 256 项 = 16 位代码段）。
 * 布局与内核 GDT 刻意错开，避免回切期间误用内核选择子：
 *   0x08 16 位数据（基 0，限 64K）   0x10 32 位数据（与内核 0x10 同语义）
 *   0x18 32 位代码                    0x800 16 位代码（基 0x8000）
 */
static uint8_t trip_gdt[257 * 8] __attribute__((aligned(16)));

/* asm 侧 lgdt 的操作数：6 字节伪描述符（limit + base）， packed 布局 */
struct bios_gdt_ptr {
    uint16_t limit;
    uint32_t base;
} __attribute__((packed));
struct bios_gdt_ptr bios_trip_gdt_ptr;

static int g_stub_ready;   /* stub 已复制 + GDT 已建（幂等） */

static void gdt_set_gate(int idx, uint32_t base, uint32_t limit,
                         uint8_t access, uint8_t gran) {
    uint8_t *d = trip_gdt + idx * 8;
    d[0] = (uint8_t)(limit & 0xFF);
    d[1] = (uint8_t)((limit >> 8) & 0xFF);
    d[2] = (uint8_t)(base & 0xFF);
    d[3] = (uint8_t)((base >> 8) & 0xFF);
    d[4] = (uint8_t)((base >> 16) & 0xFF);
    d[5] = access;
    d[6] = (uint8_t)((gran & 0xF0u) | ((limit >> 16) & 0x0Fu));
    d[7] = (uint8_t)((base >> 24) & 0xFF);
}

/* 等 8042 输入缓冲空（IBF=0）——与 keyboard.c 同参数同语义，
 * 超时即放弃继续，绝不死等（控制器坏时内核其余部分还要能跑） */
static void bt_wait_write(void) {
    int t = 100000;
    while (--t > 0 && (inb(0x64) & 0x02)) ;
}

/* 等 8042 输出缓冲满（OBF=1）；返回 0 = 等到，-1 = 超时 */
static int bt_wait_read(void) {
    int t = 100000;
    while (--t > 0 && !(inb(0x64) & 0x01)) ;
    return (t > 0) ? 0 : -1;
}

static void bt_hex2(char *p, uint8_t v) {
    const char *h = "0123456789ABCDEF";
    p[0] = h[(v >> 4) & 0xF];
    p[1] = h[v & 0xF];
}

/* 实模式回切后校验 8042 命令字节（防御性，fail-safe）。
 *
 * 背景：2026-10-20 修桌面键盘/鼠标全死时曾假设"BIOS 的 INT 10h 禁用
 * 8042 键盘/鼠标接口"，QEMU/SeaBIOS 实测证伪——本函数的 dmesg 从未
 * 触发过，命令字节根本没被改（真正根因是 thunk 的 cli/pushfd 顺序
 * 错误导致 IF=0，见 bios_thunk.asm）。保留本函数的原因是 VMware
 * SVGA BIOS 与真机 VESA BIOS 的 8042 行为尚无数据，而一旦真改，
 * 桌面就收不到一个键；改错的代价（丢键）远小于漏改的代价（全死）。
 *
 * 安全边界：
 *   - 只读不猜：读失败（OBF 超时）整体放弃，绝不写猜测值
 *   - 只恢复内核既定意图：bit0/bit1 IRQ 开、bit4/bit5 接口开
 *     （鼠标位仅 mouse_present 时）、bit6 set2->set1 翻译，
 *     与 keyboard_init/mouse_init 写入的值同源
 *   - 值没变就不写（want == cb 时零 I/O 副作用）
 */
static void ps2_restore_after_bios(void) {
    /* 先抽干：BIOS 命令的应答字节（0xFA/0xAA…）与残留 scancode 必须
     * 在读命令字之前清掉，否则 0x20 读回来的可能是数据字节 */
    int t = 100000;
    while (--t > 0 && (inb(0x64) & 0x01)) inb(0x60);

    bt_wait_write();
    outb(0x64, 0x20);                 /* 读命令字节 */
    if (bt_wait_read() != 0) return;  /* 读失败：不猜不写 */
    uint8_t cb = inb(0x60);

    uint8_t want = cb;
    want |= 0x01;                     /* IRQ1 enable */
    want &= (uint8_t)~0x10;           /* keyboard interface enable */
    want |= 0x40;                     /* translate set2 -> set1 */
    if (mouse_present()) {
        want |= 0x02;                 /* IRQ12 enable */
        want &= (uint8_t)~0x20;       /* aux interface enable */
    }

    if (want != cb) {
        char line[48];
        line[0] = 'B'; line[1] = 'I'; line[2] = 'O'; line[3] = 'S';
        line[4] = ' '; line[5] = 't'; line[6] = 'h'; line[7] = 'u';
        line[8] = 'n'; line[9] = 'k'; line[10] = ':'; line[11] = ' ';
        line[12] = '8'; line[13] = '0'; line[14] = '4'; line[15] = '2';
        line[16] = ' '; line[17] = 'c'; line[18] = 'm'; line[19] = 'd';
        line[20] = ' '; line[21] = 'b'; line[22] = 'y'; line[23] = 't';
        line[24] = 'e'; line[25] = ' '; line[26] = 'c'; line[27] = 'l';
        line[28] = 'o'; line[29] = 'b'; line[30] = 'b'; line[31] = 'e';
        line[32] = 'r'; line[33] = 'e'; line[34] = 'd'; line[35] = ':';
        line[36] = ' '; bt_hex2(line + 37, cb);
        line[39] = '-'; line[40] = '>';
        bt_hex2(line + 41, want);
        line[43] = '\n'; line[44] = '\0';
        dmesg_write(line);

        bt_wait_write();
        outb(0x64, 0x60);             /* 写命令字节 */
        bt_wait_write();
        outb(0x60, want);
    }
}

static void thunk_setup(void) {
    uint32_t n = (uint32_t)(bios_stub16_end - bios_stub16_begin);

    /* stub 落点 0x8000：引导期缓冲区早已用完（stage2 搬完内核即失效），
     * 见 bios_thunk.asm 顶部的低内存布局说明 */
    memcpy((void *)BT_STUB_DEST, bios_stub16_begin, n);

    for (uint32_t i = 0; i < sizeof(trip_gdt); i++) trip_gdt[i] = 0;
    gdt_set_gate(1,   0,          0xFFFF,      0x92, 0x00); /* 0x08: 16b data */
    gdt_set_gate(2,   0,          0xFFFFFFFFu, 0x92, 0xCF); /* 0x10: 32b data */
    gdt_set_gate(3,   0,          0xFFFFFFFFu, 0x9A, 0xCF); /* 0x18: 32b code */
    gdt_set_gate(256, BT_STUB_DEST, 0xFFFF,    0x9A, 0x00); /* 0x800: 16b code */

    bios_trip_gdt_ptr.limit = (uint16_t)(sizeof(trip_gdt) - 1);
    bios_trip_gdt_ptr.base  = (uint32_t)trip_gdt;

    /* stub 回跳地址：16 位代码编码不了 19MB 的 32 位偏移，走内存传参 */
    *(volatile uint32_t *)BT_BACK32 = (uint32_t)&bios_back32;

    g_stub_ready = 1;
}

int bios_int10(uint16_t ax, uint16_t bx, uint16_t cx, uint16_t di) {
    if (!g_stub_ready) thunk_setup();

    *(volatile uint16_t *)BT_PARM_AX = ax;
    *(volatile uint16_t *)BT_PARM_BX = bx;
    *(volatile uint16_t *)BT_PARM_CX = cx;
    *(volatile uint16_t *)BT_PARM_DI = di;

    bios_thunk_enter();

    /* 防御：BIOS 若重编程过 8042（SeaBIOS 实测没有，VMware 未知），
     * 按内核既定意图恢复——否则桌面收不到任何键。want==cb 时零副作用 */
    ps2_restore_after_bios();

    return (int)*(volatile uint16_t *)BT_RES_AX;
}

/* 上一次 INT 10h 的 BX 返回值（VBE 0x4F03 的当前模式号在 BX） */
uint16_t bios_int10_bx(void) {
    return *(volatile uint16_t *)BT_RES_BX;
}
