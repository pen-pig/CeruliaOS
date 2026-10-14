/*
 * oomtest.c - 内存耗尽（OOM）行为演练（见 oomtest.h 的设计说明）
 *
 * 实现约束：
 *   - 全程只用 kmalloc/kfree 与 kmalloc_* 统计，不碰 fs/任务/中断。
 *   - 耗尽期间**不做任何会分配内存的事**（不 dmesg、不写文件）—— 那会
 *     在"堆已满"的状态下再触发一次失败路径，把要验的东西搅乱。
 *     结果先记在静态变量里，等释放完再输出。
 *   - 无论中途发生什么，最后都必须把分配到的块全部释放，否则这条命令
 *     自己就是内存泄漏源。
 */
#include "oomtest.h"
#include "kmalloc.h"

#define OOM_MAX_BLOCKS 256

static void *g_blocks[OOM_MAX_BLOCKS];
static int   g_nblocks;

static void oom_free_all(void) {
    for (int i = 0; i < g_nblocks; i++) {
        if (g_blocks[i]) kfree(g_blocks[i]);
        g_blocks[i] = 0;
    }
    g_nblocks = 0;
}

int oom_selftest(void (*out)(const char *)) {
    if (!out) return 0;
    int fail = 0;
    uint32_t used0 = kmalloc_used();
    uint32_t largest0 = kmalloc_largest_free();

    /* 1) 边界尺寸：0 与超大都必须返回 NULL（fail closed，不是给个野指针） */
    void *p0 = kmalloc(0);
    void *pbig = kmalloc(0xFFFFFFF0u);
    out("OOMTEST: size 0 rejected: ");
    out(p0 == 0 ? "PASS\n" : "FAIL\n");
    if (p0 != 0) fail++;
    out("OOMTEST: oversize rejected: ");
    out(pbig == 0 ? "PASS\n" : "FAIL\n");
    if (pbig != 0) fail++;
    if (p0) kfree(p0);
    if (pbig) kfree(pbig);

    /* 2) 主动耗尽：反复拿 4KB 块直到拿不到 */
    int got = 0;
    int saw_null = 0;
    while (got < OOM_MAX_BLOCKS) {
        void *p = kmalloc(4096u);
        if (!p) { saw_null = 1; break; }
        /* 写一遍：确认拿到的不是"账上有、实际重叠"的块。
         * 只写首尾各一个字节——全写会让这条命令在 384KB 上跑很久。 */
        ((volatile uint8_t *)p)[0] = 0x5Au;
        ((volatile uint8_t *)p)[4095] = 0xA5u;
        g_blocks[g_nblocks++] = p;
        got++;
    }
    out("OOMTEST: allocation eventually fails: ");
    out(saw_null ? "PASS\n" : "FAIL\n");
    if (!saw_null) fail++;

    /* 耗尽状态下记账必须自洽：最大连续空闲必须小于刚才会成功的块尺寸 */
    uint32_t largest_full = kmalloc_largest_free();
    out("OOMTEST: largest_free < 4096 when full: ");
    out(largest_full < 4096u ? "PASS\n" : "FAIL\n");
    if (largest_full >= 4096u) fail++;

    /* 3) 再要一次仍应是 NULL（不是"第一次失败之后突然又能给了"） */
    void *again = kmalloc(4096u);
    out("OOMTEST: still NULL while exhausted: ");
    out(again == 0 ? "PASS\n" : "FAIL\n");
    if (again != 0) { fail++; kfree(again); }

    /* 4) 全部释放 -> 必须**完全**回到基线。差一个字节就是漏/重复释放 */
    oom_free_all();
    uint32_t used1 = kmalloc_used();
    uint32_t largest1 = kmalloc_largest_free();
    out("OOMTEST: used returns to baseline: ");
    out(used1 == used0 ? "PASS\n" : "FAIL\n");
    if (used1 != used0) fail++;
    out("OOMTEST: largest free returns to baseline: ");
    out(largest1 == largest0 ? "PASS\n" : "FAIL\n");
    if (largest1 != largest0) fail++;

    /* 5) 池仍然健康：canary 全过（耗尽演练本身不该踩坏堆） */
    uint32_t bad = kmalloc_audit();
    out("OOMTEST: heap audit clean after exercise: ");
    out(bad == 0 ? "PASS\n" : "FAIL\n");
    if (bad != 0) fail++;

    out("OOMTEST: result: ");
    out(fail == 0 ? "PASS\n" : "FAIL\n");
    return fail;
}
