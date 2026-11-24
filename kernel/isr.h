#ifndef ISR_H
#define ISR_H

#include "types.h"

#define IRQ0 32
#define IRQ1 33
#define IRQ2 34
#define IRQ11 43
#define IRQ12 44

void irq_install(void);
void pit_init(void);
void pic_restore_kernel(uint8_t master_mask, uint8_t slave_mask);

/* PIT 1000Hz 系统时钟 tick 计数（每 tick = 1ms），供启动日志真实计时 */
extern volatile uint32_t g_pit_ticks;

/* IRQ 上下文标志：任何 IRQ 处理器入口 ++、收尾 --（在 schedule() 之前！）。
 * net 的 arp_resolve 据此拒绝在 IRQ 上下文里睡等——IRQ 里睡眠会把被中断
 * 任务误标 BLOCKED 从 IRQ 栈上切走，系统级损坏。 */
extern volatile uint32_t g_in_irq;

#endif
