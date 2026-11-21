/*
 * stackchk.c - 任务内核栈的运行期守卫（见 stackchk.h 的设计说明）
 *
 * 实现约束：
 *   - 全静态、无分配、无 libc：可能被 IRQ0（中断上下文）调用。
 *   - 绝不 panic（本模块只报告，处置交给调用方）：中断上下文里停机等于
 *     把一次"栈快满了"变成整机不可用，而且会掩盖第一现场。
 *   - base 必须已经减去 guard 之后是合法可写内存，调用方（task.c）保证。
 */
#include "stackchk.h"

/* canary 内容：16 字节固定魔数。用非平凡模式，避免"全是 0"或"全是 0xFF"
 * 这种恰好等于某次真实写入内容的模式（那样会漏报）。 */
static const uint8_t KS_CANARY_PAT[KS_CANARY] = {
    0xE7u, 0x3Cu, 0x9Au, 0x51u, 0xC4u, 0x2Du, 0x8Bu, 0x66u,
    0x1Fu, 0xD8u, 0x45u, 0xA2u, 0x7Eu, 0x0Bu, 0xF3u, 0x59u
};

static uint8_t *guard_area(uint32_t base) {
    return (uint8_t *)(unsigned long)(base - KS_GUARD);
}

static uint8_t *canary_area(uint32_t base) {
    return (uint8_t *)(unsigned long)base;
}

void stackchk_arm(uint32_t base, uint32_t size) {
    if (size <= KS_CANARY) return;                 /* 尺寸不成立，别乱写 */
    uint8_t *g = guard_area(base);
    for (uint32_t i = 0; i < KS_GUARD; i++) g[i] = KS_GUARD_BYTE;

    uint8_t *c = canary_area(base);
    for (uint32_t i = 0; i < KS_CANARY; i++) c[i] = KS_CANARY_PAT[i];

    /* 其余（canary 之上到栈顶）全部填 KS_FILL，供水位统计 */
    uint8_t *p = (uint8_t *)(unsigned long)(base + KS_CANARY);
    uint32_t n = size - KS_CANARY;
    for (uint32_t i = 0; i < n; i++) p[i] = KS_FILL;
}

int stackchk_broken_layer(uint32_t base, uint32_t size) {
    if (size <= KS_CANARY) return 0;
    uint8_t *g = guard_area(base);
    for (uint32_t i = 0; i < KS_GUARD; i++)
        if (g[i] != KS_GUARD_BYTE) return 1;       /* 已经写穿栈底 */

    uint8_t *c = canary_area(base);
    for (uint32_t i = 0; i < KS_CANARY; i++)
        if (c[i] != KS_CANARY_PAT[i]) return 0;    /* 快耗尽：canary 被踩 */
    return -1;
}

int stackchk_ok(uint32_t base, uint32_t size) {
    return (stackchk_broken_layer(base, size) == -1) ? 1 : 0;
}

uint32_t stackchk_peak(uint32_t base, uint32_t size) {
    if (size <= KS_CANARY) return size;
    uint8_t *p = (uint8_t *)(unsigned long)(base + KS_CANARY);
    uint32_t n = size - KS_CANARY;
    uint32_t untouched = 0;
    /* 从未被写过的区域仍是 KS_FILL，且必然是从栈底起的**连续**一段：
     * 栈向低地址生长，用过的字节全在高地址侧。扫到第一个非填充就停。 */
    while (untouched < n && p[untouched] == KS_FILL) untouched++;
    return (size - KS_CANARY) - untouched;         /* 峰值用量 */
}

/* ---------- 自检：证明守卫"会红" ----------
 * 只会报 PASS 的守卫等于没有 —— 历史上 cmd_ls 那次栈越界之所以查了好几轮，
 * 就是因为没有任何东西能主动说"这里被写穿了"。所以自检必须真的去破坏
 * canary 和 guard，确认能被检出。
 *
 * 用独立的静态 scratch（不碰任何任务栈），破坏后立刻复原。
 * 输出走调用方给的回调（内核无 printf），所有结果行以 "STACKCHK:" 开头。 */
#define SC_SCRATCH_SIZE 1024u
static uint8_t g_sc_scratch[KS_GUARD + SC_SCRATCH_SIZE]
    __attribute__((aligned(16)));

void stackchk_selftest(void (*out)(const char *)) {
    if (!out) return;
    uint32_t base = (uint32_t)(unsigned long)&g_sc_scratch[KS_GUARD];
    uint32_t size = SC_SCRATCH_SIZE;
    int ok = 1;

    out("STACKCHK: arm -> intact: ");
    stackchk_arm(base, size);
    if (stackchk_ok(base, size)) out("PASS\n");
    else { out("FAIL\n"); ok = 0; }

    /* 1) 踩 canary（快耗尽的预警层） */
    volatile uint8_t *c = (volatile uint8_t *)(unsigned long)base;
    uint8_t saved_c = c[0];
    c[2] = 0x00u;
    out("STACKCHK: canary broken detected: ");
    if (stackchk_broken_layer(base, size) == 0) out("PASS\n");
    else { out("FAIL\n"); ok = 0; }
    c[2] = saved_c;

    /* 2) 踩 guard（真写穿栈底）。canary 此时是好的，所以判定必须落在 guard 层 */
    volatile uint8_t *g = (volatile uint8_t *)(unsigned long)(base - KS_GUARD);
    uint8_t saved_g = g[3];
    g[3] = 0x00u;
    out("STACKCHK: guard broken detected: ");
    if (stackchk_broken_layer(base, size) == 1) out("PASS\n");
    else { out("FAIL\n"); ok = 0; }
    g[3] = saved_g;

    /* 3) 水位计数：从顶部往下写 256 字节，峰值就该是 256。
     * 这里验的是"统计本身准"，不准的话 shell 里看到的用量就是假的。 */
    stackchk_arm(base, size);
    volatile uint8_t *top = (volatile uint8_t *)(unsigned long)(base + size - 256u);
    for (uint32_t i = 0; i < 256u; i++) top[i] = 0x11u;
    uint32_t peak = stackchk_peak(base, size);
    out("STACKCHK: watermark counts 256B: ");
    if (peak == 256u) out("PASS\n");
    else { out("FAIL ("); out("see value"); out(")\n"); ok = 0; }
    (void)peak;

    out("STACKCHK: result: ");
    out(ok ? "PASS\n" : "FAIL\n");
}
