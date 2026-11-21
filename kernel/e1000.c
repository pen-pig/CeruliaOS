/*
 * e1000.c - Intel 8254x（82540EM，QEMU `-device e1000`）网卡驱动
 *
 * 寄存器资料来源：Intel 8254x 开发者手册 + Linux e1000 主线驱动位定义
 * （QEMU 仿的是 82540EM，PCI 8086:100E）。
 *
 * 与 rtl8139.c 同构的职责边界：本文件只做"帧进帧出"——
 *   PCI 认领 → MMIO BAR 显式映射（BAR 常在 32MB identity 之外，
 *   见 ehci.c 同款处理）→ 复位 → MAC（EEPROM EERD 词读，RAL/RAH 兜底）→
 *   RX 32 描述符环（2048B 缓冲，单缓冲装下整个帧）→ TX 8 描述符轮转 →
 *   IRQ10/11 中断收包 + e1000_poll() 轮询收包。
 *   收到完整以太帧后上调 net_input()（kernel/net.c）；
 *   发送 e1000_send() 只管把帧塞进描述符并写 TDT 触发。
 *
 * 硬件语义（RX）：硬件从 RDH 起填缓冲，填到 RDT 前一格停（RDH==RDT = 环满）；
 * 软件读完一格就把它还回去（RDT = 下一格）。TX：软件在 TDT 处放描述符，
 * 写 TDT（= 下一格）触发发送，复用前等 status.DD。
 */
#include "e1000.h"
#include "pci.h"
#include "paging.h"
#include "kmalloc.h"
#include "idt.h"
#include "port.h"
#include "dmesg.h"
#include "net.h"
#include "isr.h"       /* g_in_irq：IRQ 上下文标志（arp_resolve 守卫用） */

#define E1000_VENDOR 0x8086
#define E1000_DEVICE 0x100E

/* ---------- MMIO 寄存器偏移 ---------- */
#define R_CTRL   0x0000    /* 设备控制：bit5 SLU 置链路 up */
#define R_STATUS 0x0008
#define R_EERD   0x0014    /* EEPROM 读：bit0 START、bit1 DONE、地址 15:8、数据 31:16 */
#define R_ICR    0x00C0    /* 中断原因（读后自清，写 1 也清） */
#define R_IMS    0x00D0    /* 中断屏蔽（使能） */
#define R_RCTL   0x0100
#define R_TCTL   0x0400
#define R_RDBAL  0x2800
#define R_RDBAH  0x2804
#define R_RDLEN  0x2808
#define R_RDH    0x2810
#define R_RDT    0x2818
#define R_TDBAL  0x3800
#define R_TDBAH  0x3804
#define R_TDLEN  0x3808
#define R_TDH    0x3810
#define R_TDT    0x3818
#define R_RAL    0x5400    /* MAC 低 4 字节 */
#define R_RAH    0x5404    /* MAC 高 2 字节（bit31 = 地址有效） */

/* CTRL 位 */
#define CTRL_SLU 0x40u     /* set link up */

/* ICR/IMS 位：RXT0 = 收包定时器（每个收到的包都会打一发） */
#define ICR_RXT0 0x80u

/* RCTL 位：EN 使能、SBP 收坏帧、UPE/MPE 收单播/组播（测试对端随配）、
 * BAM 收广播（ARP request 必需）。BSIZE 位保持 0 = 2048B 缓冲。 */
#define RCTL_EN  0x02u
#define RCTL_SBP 0x04u
#define RCTL_UPE 0x08u
#define RCTL_MPE 0x10u
#define RCTL_BAM 0x8000u

/* TCTL 位：EN 使能、PSP 短包 pad、CT 碰撞阈值、COLD 全双工间隙 */
#define TCTL_EN  0x02u
#define TCTL_PSP 0x08u
#define TCTL_CT  (0x0Fu << 4)
#define TCTL_COLD (0x40u << 12)

/* 描述符 cmd/status 位（Intel 手册 legacy 读格式；cmd 是 offset 11 的字节） */
#define TXD_EOP  0x01u     /* bit0：本描述符是帧的最后一格 */
#define TXD_IFCS 0x02u     /* bit1：硬件补 FCS */
#define TXD_RS   0x08u     /* bit3：发完报状态（DD 才能置位） */
#define TXD_DD   0x01u     /* status bit0：发送完成 */
#define RXD_DD   0x01u     /* status bit0：收包完成 */
#define RXD_EOP  0x02u     /* status bit1：本描述符是帧的最后一格 */

/* EEPROM 读轮询上限（QEMU 即刻完成；真机也不会超过微秒量级） */
#define EERD_POLL_MAX 100000

/* ---------- RX 环参数 ---------- */
#define RX_DESC_COUNT  32u
#define RX_BUF_SIZE    2048u          /* RCTL.BSIZE=0 → 单缓冲 2048B，装下整帧 */
#define RX_LEN_MIN     14u            /* 合法帧长下界（不含 CRC） */
#define RX_LEN_MAX     2040u          /* 上界（防环数据损坏） */
#define RX_DRAIN_MAX   64             /* 每次最多处理的包数（防中断活锁） */

/* ---------- TX 参数 ---------- */
#define TX_DESC_COUNT  8u
#define TX_BUF_SIZE    2048u          /* 1514 最大以太帧 + 余量 */
#define TX_WAIT_MAX    1000000u       /* 描述符忙自旋上限 */

/* 以太网帧长界限（不含 CRC） */
#define ETH_HDR_LEN    14u
#define ETH_FRAME_MAX  1514u

/* 描述符（legacy 16 字节布局，TX/RX 同一结构两种解释） */
typedef struct {
    uint32_t addr_lo;         /* 缓冲物理地址低 32 位（identity 映射 = 虚拟地址） */
    uint32_t addr_hi;         /* 高 32 位恒 0（32 位无 PAE） */
    uint16_t length;
    uint8_t  cso;
    uint8_t  cmd;
    uint8_t  status;
    uint8_t  css;
    uint16_t special;
} e1000_desc_t;

/* ---------- 驱动状态 ---------- */
static uint8_t  g_present = 0;
static uint32_t g_mmio = 0;          /* MMIO BAR 物理基址（已 identity 映射） */
static uint8_t  g_irq     = 0;
static uint8_t  g_mac[6];

/* IP 静态配置：QEMU user-net guest 缺省 10.0.2.15，宿主 10.0.2.2 */
static uint8_t  g_ip[4] = {10, 0, 2, 15};

/* 收发环 + 缓冲：kmalloc（.bss.hi identity，DMA 直投）。
 * 描述符基址硬件要求 128B 对齐，这里统一向上取 128。 */
static volatile e1000_desc_t *g_rx_ring;
static uint8_t *g_rx_buf;                      /* 32 × 2048B 连续缓冲 */
static uint8_t *g_rx_frame;                    /* 收包整帧拷出缓冲（环回安全） */
static volatile e1000_desc_t *g_tx_ring;
static uint8_t *g_tx_buf;                      /* 8 × 2048B 连续缓冲 */
static uint32_t g_rx_next;                     /* 下一个待读描述符（软件自持） */
static uint8_t  g_tx_used[TX_DESC_COUNT];
static uint32_t g_tx_next;

/* 统计（协议层计数在 net.c） */
static uint32_t g_rx_packets, g_rx_errors;
static uint32_t g_tx_packets, g_tx_busy;
static uint32_t g_irq_count;

/* 首包诊断（nic 命令显示） */
static uint8_t  g_dbg_seen;
static uint32_t g_dbg_status, g_dbg_len, g_dbg_aux;

/* asm 桩（boot/kernel_entry.asm）：中断门入口 */
extern void irq10(void);
extern void irq11(void);

/* ---------- MMIO 访问 ---------- */
static inline uint32_t rd32(uint32_t off) {
    return *(volatile uint32_t *)(unsigned long)(g_mmio + off);
}
static inline void wr32(uint32_t off, uint32_t v) {
    *(volatile uint32_t *)(unsigned long)(g_mmio + off) = v;
}

/* ---------- RX 环处理 ---------- */

static void rx_drain(void) {
    int processed = 0;
    for (int guard = 0; guard < RX_DRAIN_MAX; guard++) {
        volatile e1000_desc_t *d = &g_rx_ring[g_rx_next];
        if (!(d->status & RXD_DD)) break;      /* 读空：环内无待处理包 */

        uint16_t flen = d->length;
        if (flen < RX_LEN_MIN || flen > RX_LEN_MAX || !(d->status & RXD_EOP)) {
            /* 环数据损坏（长度出界/帧跨缓冲——2048B 缓冲下不该发生）：fail closed */
            g_rx_errors++;
        } else {
            const uint8_t *src = g_rx_buf + (uint32_t)g_rx_next * RX_BUF_SIZE;
            for (uint32_t i = 0; i < flen; i++) g_rx_frame[i] = src[i];
            g_rx_packets++;
            if (!g_dbg_seen) {
                g_dbg_seen = 1;
                g_dbg_status = d->status;
                g_dbg_len = flen;
                g_dbg_aux = rd32(R_RDH);
            }
            net_input(g_rx_frame, flen);       /* 协议处理（kernel/net.c） */
            processed = 1;
        }

        d->status = 0;
        /* 还缓冲：硬件在 RDH 处填、RDH==RDT 判满，所以 RDT 必须指向
         * "刚读空的这一格"（= 让硬件最多跑到这里）；多 +1 会立即
         * RDH==RDT 卡满整环，只收得到第一个包。 */
        wr32(R_RDT, g_rx_next);
        g_rx_next = (g_rx_next + 1u) & (RX_DESC_COUNT - 1u);
    }
    /* 每个**出口**都唤醒（与 rtl8139 同款：放循环外会饿死睡眠者） */
    if (processed) net_rx_wake();
    /* guard 耗尽：剩余包等下一次中断/轮询 */
}

/* ---------- IRQ 服务 ---------- */

void e1000_irq_service(void) {
    g_irq_count++;
    uint32_t icr = rd32(R_ICR);                /* 读后自清 */
    if (icr & ICR_RXT0) {
        rx_drain();
    } else if (icr) {
        wr32(R_ICR, icr);                      /* 其它事件（错误）只清零 */
    }
}

/* IRQ10 中断门入口（rtl8139.c 的 irq11_handler 在 IRQ11 时分发到这里） */
void irq10_handler(void) {
    g_in_irq++;
    e1000_irq_service();
    if (g_irq >= 8) outb(0xA0, 0x20);          /* 从 PIC EOI */
    outb(0x20, 0x20);                          /* 主 PIC EOI（级联始终要） */
    g_in_irq--;
}

/* 轮询收包：系统调用上下文（IF=0）里由 net.c 的阻塞循环调用 */
void e1000_poll(void) {
    if (!g_present) return;
    rx_drain();
}

/* ---------- 初始化 ---------- */

static const pci_device_t *find_device(void) {
    int n = pci_device_count();
    for (int i = 0; i < n; i++) {
        const pci_device_t *d = pci_get_device(i);
        if (d->vendor_id == E1000_VENDOR && d->device_id == E1000_DEVICE)
            return d;
    }
    return 0;
}

/* 读 MAC：优先 EEPROM EERD（地址 15:8，数据 31:16），MAC 存在词 0/1（各 4B/2B）；
 * 读不出或全 0 时回退 RAL/RAH（复位后硬件也会把 MAC 放在收帧过滤器里）。 */
static int read_mac(void) {
    uint32_t w0 = 0, w1 = 0;
    int ok = 1;
    for (uint8_t word = 0; ok && word <= 1; word++) {
        wr32(R_EERD, 1u | ((uint32_t)word << 8));
        uint32_t v = 0;
        int done = 0;
        for (int t = 0; t < EERD_POLL_MAX; t++) {
            v = rd32(R_EERD);
            if (v & 2u) { done = 1; break; }
        }
        if (!done) { ok = 0; break; }
        if (word == 0) w0 = v >> 16; else w1 = v >> 16;
    }
    if (ok) {
        g_mac[0] = (uint8_t)(w0 & 0xFFu);
        g_mac[1] = (uint8_t)((w0 >> 8) & 0xFFu);
        g_mac[2] = (uint8_t)((w0 >> 16) & 0xFFu);
        g_mac[3] = (uint8_t)((w0 >> 24) & 0xFFu);
        g_mac[4] = (uint8_t)(w1 & 0xFFu);
        g_mac[5] = (uint8_t)((w1 >> 8) & 0xFFu);
    }
    int zero = 1;
    for (int i = 0; i < 6; i++) if (g_mac[i]) zero = 0;
    if (!ok || zero) {
        uint32_t ral = rd32(R_RAL);
        uint32_t rah = rd32(R_RAH);
        g_mac[0] = (uint8_t)(ral & 0xFFu);
        g_mac[1] = (uint8_t)((ral >> 8) & 0xFFu);
        g_mac[2] = (uint8_t)((ral >> 16) & 0xFFu);
        g_mac[3] = (uint8_t)((ral >> 24) & 0xFFu);
        g_mac[4] = (uint8_t)(rah & 0xFFu);
        g_mac[5] = (uint8_t)((rah >> 8) & 0xFFu);
    }
    zero = 1;
    for (int i = 0; i < 6; i++) if (g_mac[i]) zero = 0;
    return zero ? -1 : 0;
}

/* 按 PCI 中断线注册中断门 + 放开 PIC 掩码（QEMU e1000 恒落 IRQ10/11）。
 * 其它线不挂中断（返回 -1），纯靠轮询收包。 */
static int irq_route(void) {
    if (g_irq >= 16 || g_irq == 2) return -1;
    if (g_irq != 10 && g_irq != 11) return -1;
    uint8_t vector = (g_irq < 8) ? (uint8_t)(32 + g_irq)
                                 : (uint8_t)(40 + g_irq - 8);
    idt_set_gate(vector, (uint32_t)(g_irq == 10 ? irq10 : irq11), 0x08, 0x8E);
    uint8_t m = inb(0xA1);
    m &= (uint8_t)~(1u << (g_irq - 8));
    outb(0xA1, m);
    return 0;
}

static void bufs_free(void) {
    if (g_rx_ring)  { kfree((void *)g_rx_ring);  g_rx_ring = 0; }
    if (g_rx_buf)   { kfree(g_rx_buf);   g_rx_buf = 0; }
    if (g_rx_frame) { kfree(g_rx_frame); g_rx_frame = 0; }
    if (g_tx_ring)  { kfree((void *)g_tx_ring);  g_tx_ring = 0; }
    if (g_tx_buf)   { kfree(g_tx_buf);   g_tx_buf = 0; }
}

/* 描述符环基址硬件要求 128B 对齐：多要 128B 再向上取整 */
static void *kmalloc_128(uint32_t size) {
    uint8_t *raw = kmalloc(size + 128u);
    if (!raw) return 0;
    uint32_t a = ((uint32_t)(unsigned long)raw + 127u) & ~127u;
    return (void *)(unsigned long)a;
}

int e1000_init(void) {
    const pci_device_t *d = find_device();
    if (!d) return -1;

    /* BAR0 必须是 MMIO（bit0=0）；低 3 位是类型标志，清掉得到基址。
     * BAR 常在 32MB identity 之外，必须显式映射（PCD/PWT 关缓存，
     * 与 ehci.c 同款）；寄存器窗口最远到 0x5400 区，映射 8 页覆盖。 */
    uint32_t bar = d->bar[0];
    if ((bar & 1u) != 0 || (bar & 0x6u) != 0) {
        dmesg_write("E1000: BAR0 is not 32-bit MMIO, giving up");
        return -1;
    }
    bar &= 0xFFFFFFF0u;
    if (bar == 0) {
        dmesg_write("E1000: invalid MMIO base, giving up");
        return -1;
    }
    uint32_t page = bar & ~0xFFFu;
    uint32_t npages = ((bar & 0xFFFu) == 0u) ? 8u : 9u;
    for (uint32_t i = 0; i < npages; i++) {
        if (paging_map(page + i * 0x1000u, page + i * 0x1000u,
                       PAGING_MMIO_FLAGS) != 0) {
            dmesg_write("E1000: paging_map failed, giving up");
            return -1;
        }
    }
    g_mmio = bar;
    g_irq = d->intr_line;

    /* 开 MEM 解码 + Bus Master（收发 DMA 必需） */
    uint32_t cmd = pci_read_dword(d->bus, d->dev, d->func, PCI_REG_COMMAND);
    cmd |= PCI_CMD_MEM_SPACE | PCI_CMD_BUS_MASTER;
    pci_write_dword(d->bus, d->dev, d->func, PCI_REG_COMMAND, cmd);

    /* 置链路 up（QEMU 不建 PHY 模型，此写无害；真机需要） */
    wr32(R_CTRL, rd32(R_CTRL) | CTRL_SLU);

    if (read_mac() != 0) {
        dmesg_write("E1000: cannot read MAC, giving up");
        g_mmio = 0;
        return -1;
    }

    /* ---- 收发缓冲（kmalloc，identity 映射，DMA 直投） ---- */
    g_rx_ring  = kmalloc_128(RX_DESC_COUNT * 16u);
    g_rx_buf   = kmalloc(RX_DESC_COUNT * RX_BUF_SIZE);
    g_rx_frame = kmalloc(RX_BUF_SIZE);
    g_tx_ring  = kmalloc_128(TX_DESC_COUNT * 16u);
    g_tx_buf   = kmalloc(TX_DESC_COUNT * TX_BUF_SIZE);
    if (!g_rx_ring || !g_rx_buf || !g_rx_frame || !g_tx_ring || !g_tx_buf) {
        dmesg_write("E1000: kmalloc failed for ring buffers");
        bufs_free();
        g_mmio = 0;
        return -1;
    }

    /* RX 环：全部描述符预投 2048B 缓冲；硬件从 RDH=0 填到 RDT 前一格 */
    for (uint32_t i = 0; i < RX_DESC_COUNT; i++) {
        volatile e1000_desc_t *de = &g_rx_ring[i];
        de->addr_lo = (uint32_t)(unsigned long)(g_rx_buf + i * RX_BUF_SIZE);
        de->addr_hi = 0;
        de->length  = 0;
        de->status  = 0;
        de->cmd = de->cso = de->css = 0;
        de->special = 0;
    }
    g_rx_next = 0;
    for (uint32_t i = 0; i < TX_DESC_COUNT; i++) {
        volatile e1000_desc_t *de = &g_tx_ring[i];
        de->addr_lo = (uint32_t)(unsigned long)(g_tx_buf + i * TX_BUF_SIZE);
        de->addr_hi = 0;
        de->length  = 0;
        de->status  = 0;
        de->cmd = de->cso = de->css = 0;
        de->special = 0;
        g_tx_used[i] = 0;
    }
    g_tx_next = 0;

    /* 环基址/长度（RDLEN/TDLEN 单位 = 字节，128 的倍数） */
    wr32(R_RDBAL, (uint32_t)(unsigned long)g_rx_ring);
    wr32(R_RDBAH, 0);
    wr32(R_RDLEN, RX_DESC_COUNT * 16u);
    wr32(R_RDH, 0);
    wr32(R_RDT, RX_DESC_COUNT - 1u);           /* 让硬件能填满 0..N-2 */
    wr32(R_TDBAL, (uint32_t)(unsigned long)g_tx_ring);
    wr32(R_TDBAH, 0);
    wr32(R_TDLEN, TX_DESC_COUNT * 16u);
    wr32(R_TDH, 0);
    wr32(R_TDT, 0);

    /* 收帧过滤：单播/组播/广播全收（与 rtl8139 RCR_APM|AB 同义放宽） +
     * 发送使能 */
    wr32(R_RCTL, RCTL_EN | RCTL_SBP | RCTL_UPE | RCTL_MPE | RCTL_BAM);
    wr32(R_TCTL, TCTL_EN | TCTL_PSP | TCTL_CT | TCTL_COLD);

    /* MAC 写回收帧过滤器（RAH 置地址有效位） */
    {
        uint32_t ral = (uint32_t)g_mac[0] | ((uint32_t)g_mac[1] << 8) |
                       ((uint32_t)g_mac[2] << 16) | ((uint32_t)g_mac[3] << 24);
        uint32_t rah = (uint32_t)g_mac[4] | ((uint32_t)g_mac[5] << 8) |
                       0x80000000u;
        wr32(R_RAL, ral);
        wr32(R_RAH, rah);
    }

    /* 清残留中断状态 → 注册中断门 + 放开 PIC 掩码 → 最后才开 IMS
     * （防半初始化状态进中断） */
    (void)rd32(R_ICR);
    wr32(R_ICR, 0xFFFFFFFFu);
    if (irq_route() != 0)
        dmesg_write("E1000: unusual IRQ line, run without interrupts");
    wr32(R_IMS, ICR_RXT0);

    g_present = 1;
    return 0;
}

/* ---------- 对外 API ---------- */

int e1000_send(const uint8_t *frame, uint32_t len) {
    if (!g_present || len < ETH_HDR_LEN || len > ETH_FRAME_MAX) return -1;

    /* IF 保护：防 ring0 任务态与 IRQ 收包路径并发争抢描述符轮转 */
    uint32_t efl;
    asm volatile("pushfl; popl %0" : "=r"(efl));
    asm volatile("cli");
    int rc = -1;

    int i = (int)(g_tx_next & (TX_DESC_COUNT - 1u));
    uint32_t t = TX_WAIT_MAX;
    while (g_tx_used[i] && !(g_tx_ring[i].status & TXD_DD)) {
        if (t-- == 0) { g_tx_busy++; goto out; }   /* 8 描述符全忙 */
    }

    uint8_t *buf = g_tx_buf + (uint32_t)i * TX_BUF_SIZE;
    for (uint32_t k = 0; k < len; k++) buf[k] = frame[k];
    volatile e1000_desc_t *de = &g_tx_ring[i];
    de->addr_lo = (uint32_t)(unsigned long)buf;
    de->addr_hi = 0;
    de->length  = (uint16_t)len;
    de->cso = 0;
    de->cmd = TXD_EOP | TXD_IFCS | TXD_RS;
    de->status = 0;                                /* 清残留 DD */
    de->css = 0;
    de->special = 0;
    g_tx_used[i] = 1;
    g_tx_next = (uint32_t)((i + 1) & (TX_DESC_COUNT - 1));
    wr32(R_TDT, g_tx_next);                        /* 写 TDT 触发发送 */
    g_tx_packets++;
    rc = 0;

out:
    if (efl & 0x200u) asm volatile("sti");
    return rc;
}

int      e1000_present(void)   { return g_present; }
uint32_t e1000_mmio_base(void) { return g_mmio; }
uint8_t  e1000_irq(void)       { return g_irq; }
const uint8_t *e1000_mac(void) { return g_mac; }

void e1000_mac_str(char *out) {
    static const char hex[] = "0123456789ABCDEF";
    for (int i = 0; i < 6; i++) {
        out[i * 3]     = hex[g_mac[i] >> 4];
        out[i * 3 + 1] = hex[g_mac[i] & 0xF];
        out[i * 3 + 2] = (i == 5) ? '\0' : ':';
    }
}

const uint8_t *e1000_ip(void) { return g_ip; }

void e1000_set_ip(uint8_t a, uint8_t b, uint8_t c, uint8_t d) {
    g_ip[0] = a; g_ip[1] = b; g_ip[2] = c; g_ip[3] = d;
}

void e1000_ip_str(char *out) {
    for (int i = 0; i < 4; i++) {
        uint8_t v = g_ip[i];
        if (v >= 100) {
            *out++ = (char)('0' + v / 100);
            *out++ = (char)('0' + (v / 10) % 10);
            *out++ = (char)('0' + v % 10);
        } else if (v >= 10) {
            *out++ = (char)('0' + v / 10);
            *out++ = (char)('0' + v % 10);
        } else {
            *out++ = (char)('0' + v);
        }
        if (i < 3) *out++ = '.';
    }
    *out = '\0';
}

uint32_t e1000_rx_packets(void)   { return g_rx_packets; }
uint32_t e1000_rx_errors(void)    { return g_rx_errors; }
uint32_t e1000_tx_packets(void)   { return g_tx_packets; }
uint32_t e1000_tx_busy(void)      { return g_tx_busy; }
uint32_t e1000_irq_count(void)    { return g_irq_count; }

void e1000_rx_debug(uint32_t *status, uint32_t *len, uint32_t *aux) {
    if (status) *status = g_dbg_seen ? g_dbg_status : 0;
    if (len)    *len    = g_dbg_seen ? g_dbg_len : 0;
    if (aux)    *aux    = g_dbg_seen ? g_dbg_aux : 0;
}
