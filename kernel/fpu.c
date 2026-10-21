/*
 * fpu.c - x87 FPU 初始化与自检（步骤 5e）
 *
 * 为什么需要显式初始化：上电/复位后 FPU 控制字内容未定义
 * （intel SDM Vol.1 8.1.5）。不 fninit 就做浮点运算，精度控制
 * 与异常屏蔽位全是随机值——可能 #MF 或算错。
 *
 * 探测（SDR 推荐流程）：fninit; fnstenv; 状态字==0 => 有 FPU。
 * i686 起 CR0.EM 必须为 0，无 FPU 时这两条指令是 NOP 不异常，
 * 所以探测只能靠哨兵值，不能靠陷阱。
 */
#include "fpu.h"
#include "dmesg.h"

/* fnstenv 的 28 字节布局：控制字 +0，状态字 +4（16 位字段各占 4 字节槽） */
typedef struct {
    uint32_t ctrl;
    uint32_t status;
    uint32_t tag;
    uint32_t ip;
    uint32_t ipsel;
    uint32_t op;
    uint32_t opsel;
} __attribute__((packed)) fpu_env_t;

static int g_fpu_ok;                          /* 探测结果缓存 */

/* gcc 的 387 后端要求这些运算出现在独立函数里（跨 basic block 的
 * 浮点比较会让 gcc 生成 fnstsw/fwait 序列，全都合法）。 */
static double f_add(double a, double b) { return a + b; }
static double f_sub(double a, double b) { return a - b; }
static double f_mul(double a, double b) { return a * b; }
static double f_div(double a, double b) { return a / b; }
static double f_neg(double a)        { return -a; }

/* CPUID.1:EDX bit0 = FPU on chip（Intel SDM Vol.2A CPUID 指令说明）。
 * 这是架构规定的权威判定；哨兵探测（fninit+fnstenv）是行为佐证。
 * 2026-10-20 VMware 实测：哨兵失败但 CPUID 未见数据——两者都打，
 * 哪个说谎一看便知。 */
static int cpuid_has_fpu(void) {
    uint32_t eax, ebx, ecx, edx;
    asm volatile(
        "xor %%eax, %%eax\n"
        "cpuid\n"                       /* leaf 0：最大叶号在 eax */
        "cmp $1, %%eax\n"
        "jb 1f\n"
        "mov $1, %%eax\n"
        "xor %%ecx, %%ecx\n"
        "cpuid\n"                       /* leaf 1：特性位在 edx */
        "mov %%edx, %[edx]\n"
        "jmp 2f\n"
        "1:\n"
        "mov $-1, %[edx]\n"             /* 老 CPU：无 leaf 1 */
        "2:\n"
        : [edx] "=r"(edx), "=a"(eax), "=b"(ebx), "=c"(ecx)
        :
        : "cc");
    return (edx & 1u) != 0;
}

static void fpu_diag_line(const char *tag, uint32_t v) {
    static const char *h = "0123456789ABCDEF";
    char line[48];
    int i = 0;
    const char *p = "FPU: ";
    while (*p) line[i++] = *p++;
    while (*tag) line[i++] = *tag++;
    line[i++] = '=';
    line[i++] = '0'; line[i++] = 'x';
    for (int b = 28; b >= 0; b -= 4) line[i++] = h[(v >> b) & 0xF];
    line[i++] = '\n';
    line[i] = '\0';
    dmesg_write(line);
}

int fpu_init(void) {
    fpu_env_t env;
    env.ctrl   = 0x5A5A5A5Au;                 /* 哨兵：无 FPU 时保持不变 */
    env.status = 0xA5A5A5A5u;
    asm volatile(
        "fninit        \n"                    /* CW=0x037F，状态/tag 清零 */
        "fnstenv %0   \n"
        : "=m"(env)
        :
        : "memory"
    );
    /* fninit 把控制字写成 0x037F、状态字清零。fnstenv 的 32 位槽高位
     * 是保留位（Intel SDM Vol.1 8.1.8）：QEMU 写 0，VMware 写 0xFFFF。
     * 必须 mask 0xFFFF 再比，否则 VMware 上哨兵永远假阴性。 */
    int sentinel_ok = ((env.ctrl & 0xFFFFu) == 0x037Fu)
                   && ((env.status & 0xFFFFu) == 0u);
    int cpuid_ok = cpuid_has_fpu();
    /* 双证据都进 dmesg：QEMU/VMware/真机行为分歧时不用重跑就能定位 */
    fpu_diag_line("cpuid.edx0", (uint32_t)cpuid_ok);
    fpu_diag_line("sentinel.ok", (uint32_t)sentinel_ok);
    fpu_diag_line("sentinel.ctrl", env.ctrl);
    fpu_diag_line("sentinel.status", env.status);
    /* CPUID 是架构权威：它说有就有（哨兵失败 = fninit/fnstenv 被某种
     * 方式干扰，如实记录但不停机）；它说没有才真没有。 */
    g_fpu_ok = cpuid_ok;
    return g_fpu_ok ? 0 : -1;
}

int fpu_available(void) { return g_fpu_ok; }

/*
 * 自检：精确可预期的算式，不涉及三角/超越函数（它们依赖 8087 精度
 * 细节，跨实现比较脆弱）。除零故意不测——默认 CW 屏蔽掉 #Z 异常后
 * 结果是 ±Inf，但"Inf 可比较"在不同 FPU 实现上有历史差异。
 */
/* 静默输出：开机自检路径用 */
static void fpu_null_puts(const char *s) { (void)s; }

int fpu_selftest(void (*out)(const char *)) {
    if (out == 0) out = fpu_null_puts;
    int fail = 0;

    if (!g_fpu_ok) { out("fpu: not present\n"); return 1; }
    out("fpu self-test:\n");

    /* 每项：算式 / 期望值 / 描述。0.5 幂的浮点数可精确表示，
     * 结果无舍入误差，比较是精确的。 */
    struct { double (*fn)(double, double); double a, b, want; const char *name; } t[] = {
        { f_add, 1.5,  2.25, 3.75,  "add 1.5+2.25 = 3.75" },
        { f_sub, 3.75, 0.25, 3.5,   "sub 3.75-0.25 = 3.5" },
        { f_mul, 1.25, 4.0,  5.0,   "mul 1.25*4 = 5" },
        { f_div, 7.5,  2.5,  3.0,   "div 7.5/2.5 = 3" },
        { 0,     0,    0,    0,     0 }
    };
    for (int i = 0; t[i].name != 0; i++) {
        double r = t[i].fn(t[i].a, t[i].b);
        int ok = (r == t[i].want);            /* 0.5 幂：精确相等 */
        out("  ");
        out(t[i].name);
        out(ok ? ": OK\n" : ": FAIL\n");
        if (!ok) fail++;
    }

    /* 符号与比较路径：neg 结果要和期望一致 */
    {
        double r = f_neg(2.5);
        int ok = (r == -2.5) && !(r == 2.5);
        out("  neg -2.5 sign: ");
        out(ok ? "OK\n" : "FAIL\n");
        if (!ok) fail++;
    }

    out(fail == 0 ? "  result: PASS\n" : "  result: FAIL\n");
    return fail;
}
