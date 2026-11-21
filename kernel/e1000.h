/*
 * e1000.h - Intel 8254x（QEMU `-device e1000`）网卡驱动：帧收发层
 *
 * 编程模型与 rtl8139 驱动（rtl8139.h）同构：PCI 认领 → MMIO BAR 映射 →
 * 复位 → RX/TX 描述符环（DMA 缓冲来自内核堆，identity 映射）→ IRQ 收包 +
 * e1000_poll() 轮询收包。完整以太帧上调 net_input()，发送只负责提交描述符。
 *
 * API 与 rtl8139 一一对应，net.c 的 netdev_* 统一层据此二选一
 * （e1000 优先，缺则 rtl8139）。QEMU 未挂 e1000 时 init 返回 -1，
 * 调用方静默跳过。
 */
#ifndef E1000_H
#define E1000_H

#include "types.h"

/* 初始化：PCI 认领 8086:100E → 开 MEM 解码 + BusMaster → 显式映射 MMIO BAR
 * → 复位 → EEPROM/RAL 读 MAC → 分配 RX/TX 环 → 使能收发 → 注册 IRQ。
 * 返回 0 = 成功；-1 = 无此设备/无 MMIO BAR/复位失败/缓冲不足。 */
int e1000_init(void);

/* 发送一个以太网帧（含 14 字节以太网头，不含 CRC——硬件自动补）。
 * 同步接口：拷入轮转描述符缓冲后立即返回。可在 IRQ 上下文调用（内部 IF 保护）。
 * 返回 0 = 已提交；-1 = 参数非法/未初始化/描述符忙超时。 */
int e1000_send(const uint8_t *frame, uint32_t len);

/* 轮询收包：排水 RX 环（无待处理包时立即返回）。系统调用上下文里
 * net.c 的阻塞等待循环用它推进收包。 */
void e1000_poll(void);

/* IRQ 服务核心：读 ICR、RXT0 时排水 RX 环。不含 EOI（由桩/分发方补），
 * 由 irq10_handler 和 rtl8139.c 的 irq11_handler（IRQ11 分发）调用。 */
void e1000_irq_service(void);

/* 状态查询（未初始化时 present=0、mac 全 0） */
int      e1000_present(void);
uint32_t e1000_mmio_base(void);   /* MMIO BAR 物理基址（identity 映射） */
uint8_t  e1000_irq(void);
const uint8_t *e1000_mac(void);

/* 把 MAC 格式化成 "XX:XX:XX:XX:XX:XX"（out 至少 18 字节） */
void e1000_mac_str(char *out);

/* ---- IP 配置（与 rtl8139 同义；缺省 10.0.2.15） ---- */
const uint8_t *e1000_ip(void);
void e1000_set_ip(uint8_t a, uint8_t b, uint8_t c, uint8_t d);

/* "a.b.c.d"（out 至少 16 字节） */
void e1000_ip_str(char *out);

/* ---- 统计（nic 命令 / E2E 断言用） ---- */
uint32_t e1000_rx_packets(void);
uint32_t e1000_rx_errors(void);
uint32_t e1000_tx_packets(void);
uint32_t e1000_tx_busy(void);
uint32_t e1000_irq_count(void);

/* 首包诊断（status/len/辅助信息，未收到过包时为 0）；与 rtl8139_rx_debug 对齐 */
void e1000_rx_debug(uint32_t *status, uint32_t *len, uint32_t *aux);

#endif
