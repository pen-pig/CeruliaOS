/*
 * oomtest.h - 内存耗尽（OOM）行为演练
 *
 * 为什么需要：kmalloc 池只有 384KB，任何"分配失败"的路径在测试机上几乎
 * 走不到 —— 平时堆是空的，什么都分配得到。真到内存紧张时才发现某条路径
 * 拿到 NULL 就直接用，那就晚了。所以**主动把堆耗尽**，在压力状态下验证
 * 分配器的契约仍然成立。
 *
 * 验的是三件事：
 *   1. 边界尺寸是 fail closed 的（0 和超大都返回 NULL，不是返回个野指针）
 *   2. 耗尽之后返回 NULL 而不是崩，且记账（used/largest_free）自洽
 *   3. 全部释放后**完全**回到基线 —— 差一个字节就是漏了块/重复释放
 */
#ifndef OOMTEST_H
#define OOMTEST_H

#include "types.h"

/* 逐项打印 "OOMTEST: <name>: PASS/FAIL"，末尾 "OOMTEST: result: PASS/FAIL"。
 * out 为 NULL 时静默返回。返回值：0 = 全部通过，非 0 = 失败项数。 */
int oom_selftest(void (*out)(const char *));

#endif
