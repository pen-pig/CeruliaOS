/*
 * stackchk.h - 任务内核栈的运行期守卫（水位 + canary + guard）
 *
 * 为什么需要：静态栈预算（tools/check_stack.py，单帧 <= 12KB）只能看编译期
 * 估算值，抓不到运行期的真实深度（递归、条件分支里的深路径），更抓不到
 * **写穿**：TASK_KSIZE 只有 16KB，越界写会静默改掉 .bss.kstack 里相邻任务
 * 的栈、甚至别的静态缓冲。历史上 cmd_ls 的 entries[64] 就写穿过 fs.c 的
 * ro_cwd，症状是提示符变成 `[A.TXT]`——看起来像文件系统坏了，查了好几轮
 * 才发现是栈。这类 bug 的成本全在定位上，所以要在运行期就把它钉住。
 *
 * 三层，成本从低到高：
 *   1. guard（栈底之下 KS_GUARD 字节）：真越界才会改到，坏了就是出事了
 *   2. canary（栈底 KS_CANARY 字节）：快耗尽时被改，是"预警"
 *   3. 水位（0xA5 填充 + 扫描）：还没坏但快满了，用来看真实用量
 *
 * 布局（低地址 -> 高地址）：
 *   [guard KS_GUARD][canary KS_CANARY][0xA5 填充 ... ][栈顶 kstack_top]
 *   栈向低地址生长，越界先踩 guard，再往上是 canary。
 */
#ifndef STACKCHK_H
#define STACKCHK_H

#include "types.h"

/* 栈底之下的保护字节数。8 个任务 * 64B = 512B，相对 .bss.kstack 的
 * 0x904000..0xA00000 窗口可以忽略。 */
#define KS_GUARD   64u
/* 栈底 canary 字节数（紧贴 guard 之上） */
#define KS_CANARY  16u

/* 填充模式。0xA5 是常见的"未使用栈"填充值；guard 用 0x5A 与其区分，
 * 免得"整块都是同一个字节"时分辨不出是谁坏了。 */
#define KS_FILL    0xA5u
#define KS_GUARD_BYTE 0x5Au

/* 装载守卫：填充 [base, base+size) 为 KS_FILL，栈底放 canary，
 * 栈底之下放 guard。必须在任务开始用栈**之前**调用。 */
void stackchk_arm(uint32_t base, uint32_t size);

/* 1 = guard 与 canary 都完好；0 = 已被改写（栈溢出或越界写）。
 * 开销是几十字节的比较，可以在 IRQ0 里对全部任务周期性调用。 */
int stackchk_ok(uint32_t base, uint32_t size);

/* 分别报告哪一层坏了，给诊断用（dmesg 要能说清是"快满了"还是"已经写穿"）。
 * -1 表示完好；0 = canary 坏；1 = guard 坏。 */
int stackchk_broken_layer(uint32_t base, uint32_t size);

/* 历史最大用量（字节）。做法是扫"从栈底起还剩多少连续的 KS_FILL"。
 * **只在 shell 命令里调用**：这是 16KB 的线性扫描，别放进中断路径。 */
uint32_t stackchk_peak(uint32_t base, uint32_t size);

/* 自检：真的去踩 canary 与 guard，确认能被检出，并验水位计数。
 * 用独立的静态 scratch，不碰任何任务栈。结果行以 "STACKCHK:" 开头。
 * out 为 NULL 时静默返回。 */
void stackchk_selftest(void (*out)(const char *));

#endif
