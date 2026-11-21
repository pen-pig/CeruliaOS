/*
 * ata.c - ATA/IDE PIO 驱动（主/副总线，四驱动器，硬件兼容加固）
 *
 * 兼容性措施：
 *   - 支持 primary(0x1F0)/secondary(0x170) 两条总线，drive 0-3；
 *   - 选盘后读 4 次备用状态口产生 400ns 延迟（等待驱动器就绪）；
 *   - 浮动总线检测：状态口读回 0xFF 说明驱动器不存在，快速失败，
 *     避免对空插槽做长时间轮询超时；
 *   - 命令完成后逐位检查 BSY/DRQ/ERR/DF，任何异常立即返回错误；
 *   - 写命令在数据阶段结束后额外等待 BSY 清除（flush）。
 */
#include "ata.h"
#include "ahci.h"
#include "usbmsc.h"
#include "nvme.h"
#include "port.h"
#include "tty.h"
#include "task.h"

#define ATA_STAT_BSY  0x80
#define ATA_STAT_DRDY 0x40
#define ATA_STAT_DF   0x20
#define ATA_STAT_DRQ  0x08
#define ATA_STAT_ERR  0x01

/* 命令后轮询上限（PIO 下远大于任何真实驱动器需求） */
#define ATA_TIMEOUT 4000000u

/* drive 0/1 = 主总线主/从盘；2/3 = 副总线主/从盘 */
static uint16_t ata_io_base(uint8_t drive) {
    return (drive >= 2) ? 0x170 : 0x1F0;
}

/* 400ns 延迟：读 4 次备用状态口（base+0x206），不清除中断状态 */
static void ata_400ns(uint16_t base) {
    for (int i = 0; i < 4; i++) inb(base + 0x206);
}

/* 选盘；浮动总线（0xFF）或超时视为驱动器不存在，返回 -1 */
static int ata_select(uint8_t drive) {
    uint16_t base = ata_io_base(drive);
    outb(base + 6, (drive & 1) ? 0xF0 : 0xE0);
    ata_400ns(base);

    uint32_t t = ATA_TIMEOUT;
    uint8_t status = inb(base + 7);
    while (status & ATA_STAT_BSY) {
        if (status == 0xFF) return -1;
        if (t-- == 0) return -1;
        status = inb(base + 7);
    }
    if (status == 0xFF) return -1;      /* 空插槽：数据线浮动 */
    return 0;
}

/* 等 BSY 清除；检查错误位 */
static int ata_wait_idle(uint16_t base) {
    uint32_t t = ATA_TIMEOUT;
    uint8_t status = inb(base + 7);
    while (status & ATA_STAT_BSY) {
        if (status == 0xFF) return -1;
        if (t-- == 0) return -1;
        status = inb(base + 7);
    }
    if (status & ATA_STAT_ERR) return -1;
    if (status & ATA_STAT_DF) return -1;
    return 0;
}

/* 等 DRQ 置位（数据就绪）；期间 ERR/DF 立即失败 */
static int ata_wait_drq(uint16_t base) {
    uint32_t t = ATA_TIMEOUT;
    for (;;) {
        uint8_t status = inb(base + 7);
        if (status & ATA_STAT_ERR) return -1;
        if (status & ATA_STAT_DF) return -1;
        if (status & ATA_STAT_DRQ) return 0;
        if (status == 0xFF) return -1;
        if (t-- == 0) return -1;
    }
}

/*
 * 驱动器存在性检测：IDENTIFY DEVICE 验证。
 *
 * 仅靠浮动总线 0xFF 判存不可靠：QEMU 及部分控制器对空插槽
 * 返回 0x00（BSY/DRQ 均不置位），ata_select 会误判为存在，
 * 随后 READ SECTORS 无人应答只能等满超时（启动扫描明显变慢）。
 *
 * 标准做法：发 0xEC 后——
 *   - 空插槽无响应（状态持续 0x00/0xFF）→ 不存在；
 *   - DRQ 置位 → ATA 设备；
 *   - ERR 置位（如 ATAPI 的 ABRT）→ 设备存在但不支持 IDENTIFY。
 */
int ata_drive_present(uint8_t drive) {
    /* NVMe 区间编号最大，必须**先判**：drive 16+ 若先落到 USBMSC 分支
     * 会被当成 unit 4+ 而越界返回 0，NVMe 盘就永远"不存在"。 */
    if (drive >= NVME_DRIVE_BASE) {
        return nvme_ns_present((uint8_t)(drive - NVME_DRIVE_BASE)) ? 1 : 0;
    }
    if (drive >= USBMSC_DRIVE_BASE) {
        return usbmsc_present((uint8_t)(drive - USBMSC_DRIVE_BASE)) ? 1 : 0;
    }
    if (drive >= AHCI_DRIVE_BASE) {
        uint8_t p = (uint8_t)(drive - AHCI_DRIVE_BASE);
        if (p >= AHCI_MAX_PORTS) return 0;
        return ahci_port_present(p);
    }
    if (drive > 3) return 0;
    uint16_t base = ata_io_base(drive);
    if (ata_select(drive) != 0) return 0;

    outb(base + 2, 0);                          /* sector count = 0 */
    outb(base + 3, 0);
    outb(base + 4, 0);
    outb(base + 5, 0);
    outb(base + 7, 0xEC);                       /* IDENTIFY DEVICE */

    uint32_t t = ATA_TIMEOUT;
    for (;;) {
        uint8_t status = inb(base + 7);
        if (status == 0xFF) return 0;           /* 浮动总线 */
        if (status == 0x00) return 0;           /* 空插槽：命令无应答 */
        if (status & ATA_STAT_ERR) {
            /* ATAPI 签名：LBA mid=0x14 / LBA high=0xEB（ATA/ATAPI-6 §9.10.2）。
             * VMware 空 IDE 插槽也回 ERR，但签名不是 ATAPI——必须验，
             * 否则空插槽被误判成"存在"（2026-10-21 实测 drive 2/3 误报）。 */
            uint8_t sig_mid = inb(base + 4);
            uint8_t sig_hi  = inb(base + 5);
            return (sig_mid == 0x14 && sig_hi == 0xEB) ? 1 : 0;
        }
        if (status & ATA_STAT_DRQ) {            /* ATA 设备：读 IDENTIFY 256 words
                                                 * 必须清掉数据，否则 DRQ 滞留 + FIFO
                                                 * 残留，下一命令先取到脏数据。
                                                 * 同时验 model 字段非全零——VMware
                                                 * 空插槽 DRQ 后读出全零数据。 */
            uint16_t id[256];
            for (int i = 0; i < 256; i++) id[i] = inw(base);
            /* model 在 words 27-46（偏移 54-92 字节），全零 = 假设备 */
            int nonzero = 0;
            for (int i = 27; i <= 46; i++) {
                if (id[i] != 0) { nonzero = 1; break; }
            }
            return nonzero ? 1 : 0;
        }
        if (status & ATA_STAT_BSY) {            /* 忙：继续等结果 */
            if (t-- == 0) return 0;
            continue;
        }
        /* 无 BSY/DRQ/ERR 且非 0：反复采样少量次数后放弃 */
        if (t-- == 0) return 0;
        if (t < ATA_TIMEOUT - 16) return 0;     /* 17 次无有效应答：判空 */
    }
}

/* ============================================================
 * 容量查询（ata_capacity）
 *
 * 为什么要在块层提供它：format 必须知道盘有多大才能决定文件系统布局。
 * 之前 ext4_format 硬编码 1MB、ntfs_format 硬编码 2MB——在 16MB 的数据盘上
 * 只用掉 6%/12%，剩下的永远摸不到。没有容量接口就只能写死，写死就不是
 * 生产系统。
 *
 * 四条通路各自已经有容量信息（AHCI 有 IDENTIFY 缓冲、NVMe 有 NSZE、
 * USB-MSC 有 READ CAPACITY），这里只是把它们统一成一个入口。
 *
 * fail closed：拿不到容量返回 0，让调用方拒绝格式化，绝不猜一个值写盘。
 * ============================================================ */

/* PATA：发 IDENTIFY DEVICE 并收 256 words（buf 需 512 字节）。
 * 返回 0 成功；-1 = 不存在 / ATAPI（无 ATA 数据阶段）/ 超时。 */
static int ata_identify_pata(uint8_t drive, uint16_t *buf) {
    if (drive > 3) return -1;
    uint16_t base = ata_io_base(drive);
    if (ata_select(drive) != 0) return -1;

    outb(base + 2, 0);
    outb(base + 3, 0);
    outb(base + 4, 0);
    outb(base + 5, 0);
    outb(base + 7, 0xEC);                       /* IDENTIFY DEVICE */

    uint32_t t = ATA_TIMEOUT;
    for (;;) {
        uint8_t status = inb(base + 7);
        if (status == 0xFF || status == 0x00) return -1;
        if (status & ATA_STAT_ERR) return -1;   /* ATAPI 等：无 ATA 数据阶段 */
        if (status & ATA_STAT_DRQ) break;
        if (--t == 0) return -1;
    }
    for (int i = 0; i < 256; i++) {
        uint16_t w = inw(base);
        if (buf) buf[i] = w;
    }
    if (ata_wait_idle(base) != 0) return -1;
    /* 字 0 bit15：1 = ATAPI 设备，那份数据不是 ATA IDENTIFY，不能用 */
    if (buf && (buf[0] & 0x8000u)) return -1;
    return 0;
}

uint32_t ata_capacity(uint8_t drive) {
    if (drive >= NVME_DRIVE_BASE)
        return nvme_ns_sectors((uint8_t)(drive - NVME_DRIVE_BASE));
    if (drive >= USBMSC_DRIVE_BASE)
        return usbmsc_sectors((uint8_t)(drive - USBMSC_DRIVE_BASE));
    if (drive >= AHCI_DRIVE_BASE) {
        uint8_t p = (uint8_t)(drive - AHCI_DRIVE_BASE);
        if (p >= AHCI_MAX_PORTS) return 0;
        return ahci_capacity(p);
    }
    if (drive > 3) return 0;

    /* 512B IDENTIFY 缓冲。放在 static（而非栈）：内核栈只有几 KB，
     * 且这块只在 format/diskinfo 时偶尔用一次。 */
    static uint16_t id[256];
    if (ata_identify_pata(drive, id) != 0) return 0;

    uint32_t n = 0;
    /* 字 83 bit10 = LBA48 支持；支持时字 100-103 是 64 位扇区数 */
    if (id[83] & 0x0400u) {
        n = (uint32_t)id[100] | ((uint32_t)id[101] << 16);
    }
    if (n == 0) {
        n = (uint32_t)id[60] | ((uint32_t)id[61] << 16);   /* LBA28 */
    }
    if (n == 0 || n == 0x0FFFFFFFu) return 0;   /* 0 与 LBA28 截断哨兵都不可信 */
    if (n > 0xFFFFFFFEu) n = 0xFFFFFFFEu;       /* lba 是 uint32：>2TB 截断 */
    return n;
}

/* 发一条 48 位 LBA 的 PIO 命令（READ SECTORS EXT 0x24 / WRITE EXT 0x34）。
 *
 * 为什么需要：ata_capacity() 在设备支持 LBA48 时读的是字 100-103（64 位
 * 扇区数），返回的上限是 0xFFFFFFFE（2TB）；而读写一直只发 28 位 LBA 的
 * 0x20/0x30。LBA28 的编址上限是 0x0FFFFFFF（128GB）——超过它的扇区号，
 * 高 4 位会被 `(lba >> 24) & 0x0F` 静默丢掉，于是"读第 3 亿个扇区"变成
 * "读第 3 亿 mod 2^28 个扇区"。这不是报错，是**读到错误的扇区还返回成
 * 功**，文件系统会把别人的元数据当自己的解析。
 * 现在：超过 LBA28 上限一律走 48 位命令；设备不支持则命令被 ABRT，
 * ata_wait_idle 会看到 ERR 并返回 -1（fail closed，而不是读错盘）。
 *
 * 48 位寄存器写法（ATA/ATAPI-6 7.x）：每个寄存器先写高字节、再写低字节，
 * 最后 device 寄存器只带 LBA 位（bit6）与从设备位（bit4），不再放 LBA
 * 的 24-27 位（那 4 位改由 LBA Low 的高字节字节承载）。 */
static void ata_cmd_lba48(uint16_t base, uint8_t drive, uint32_t lba,
                          uint32_t count, uint8_t cmd) {
    outb(base + 2, (uint8_t)((count >> 8) & 0xFF));      /* 扇区数 高 */
    outb(base + 2, (uint8_t)(count & 0xFF));             /* 扇区数 低 */
    outb(base + 3, (uint8_t)((lba >> 24) & 0xFF));       /* LBA 24-31 */
    outb(base + 3, (uint8_t)(lba & 0xFF));               /* LBA 0-7   */
    outb(base + 4, 0);                                   /* LBA 32-39：lba 是 32 位，恒 0 */
    outb(base + 4, (uint8_t)((lba >> 8) & 0xFF));        /* LBA 8-15  */
    outb(base + 5, 0);                                   /* LBA 40-47：同上 */
    outb(base + 5, (uint8_t)((lba >> 16) & 0xFF));       /* LBA 16-23 */
    outb(base + 6, ((drive & 1) ? 0x50 : 0x40));         /* LBA48 */
    outb(base + 7, cmd);
}

/* LBA28 编址上限：28 位可用（0x0FFFFFFF） */
#define ATA_LBA28_MAX   0x0FFFFFFFu

/* ============ 块设备互斥 ============
 * ata_read_sector/ata_write_sector 是四条驱动通路（ATA/AHCI/NVMe/USB-MSC）
 * 的共用入口：抢占调度下两个任务并发盘 IO，PIO FIFO/DMA 缓冲会被对方
 * 覆写任意内核内存。文件级互斥：忙则睡在 g_blk_wq 上（2ms 定时兜底 +
 * IO 完成方 wake_all 精确唤醒）。不用 task_lock：PIT tick 依赖 IF=1，
 * 关中断互斥会把调度器锁死。 */
volatile int g_blk_busy = 0;
static wait_queue_t g_blk_wq = { -1 };

static void blk_lock(void) {
    for (;;) {
        asm volatile("cli" ::: "memory");        /* 判定与置位原子 */
        if (g_blk_busy == 0) {
            g_blk_busy = 1;
            asm volatile("sti" ::: "memory");
            return;
        }
        task_sleep(&g_blk_wq, 2);                /* 忙：睡 2ms 重试（定时兜底） */
    }
}

static void blk_unlock(void) {
    asm volatile("cli" ::: "memory");
    g_blk_busy = 0;
    task_wake_all(&g_blk_wq);                    /* 唤醒排队者重判 */
    asm volatile("sti" ::: "memory");
}

/* 每条 PIO 命令最多带 64 个扇区（32KB）：足够大以摊薄命令开销，
 * 又不会让单次轮询/传输时间失控。 */
#define ATA_CHUNK 64u

/* 多扇区 PIO 读（PATA）：一条命令带 count 个扇区，DRQ 每扇区置一次 */
static int pata_read_multi(uint8_t drive, uint32_t lba, uint32_t count,
                           uint8_t *buffer) {
    uint16_t base = ata_io_base(drive);
    if (drive > 3) return -1;
    if (ata_select(drive) != 0) return -1;

    while (count > 0) {
        uint32_t n = (count > ATA_CHUNK) ? ATA_CHUNK : count;
        if (lba <= ATA_LBA28_MAX && lba + n - 1 <= ATA_LBA28_MAX) {
            outb(base + 2, (uint8_t)n);                 /* sector count */
            outb(base + 3, (uint8_t)(lba & 0xFF));
            outb(base + 4, (uint8_t)((lba >> 8) & 0xFF));
            outb(base + 5, (uint8_t)((lba >> 16) & 0xFF));
            outb(base + 6, ((drive & 1) ? 0xF0 : 0xE0) | ((lba >> 24) & 0x0F));
            outb(base + 7, 0x20);                       /* READ SECTORS */
        } else {
            ata_cmd_lba48(base, drive, lba, n, 0x24);   /* READ SECTORS EXT */
        }
        if (ata_wait_idle(base) != 0) return -1;
        for (uint32_t s = 0; s < n; s++) {
            if (ata_wait_drq(base) != 0) return -1;
            uint8_t *p = buffer + s * 512;
            for (int i = 0; i < 256; i++) {
                uint16_t data = inw(base);
                p[i * 2] = (uint8_t)(data & 0xFF);
                p[i * 2 + 1] = (uint8_t)((data >> 8) & 0xFF);
            }
        }
        lba += n;
        buffer += n * 512;
        count -= n;
    }
    return 0;
}

/* 多扇区 PIO 写（PATA）：DRQ 每扇区置一次，全部写完等 BSY 清（flush） */
static int pata_write_multi(uint8_t drive, uint32_t lba, uint32_t count,
                            const uint8_t *buffer) {
    uint16_t base = ata_io_base(drive);
    if (drive > 3) return -1;
    if (ata_select(drive) != 0) return -1;

    while (count > 0) {
        uint32_t n = (count > ATA_CHUNK) ? ATA_CHUNK : count;
        if (lba <= ATA_LBA28_MAX && lba + n - 1 <= ATA_LBA28_MAX) {
            outb(base + 2, (uint8_t)n);                 /* sector count */
            outb(base + 3, (uint8_t)(lba & 0xFF));
            outb(base + 4, (uint8_t)((lba >> 8) & 0xFF));
            outb(base + 5, (uint8_t)((lba >> 16) & 0xFF));
            outb(base + 6, ((drive & 1) ? 0xF0 : 0xE0) | ((lba >> 24) & 0x0F));
            outb(base + 7, 0x30);                       /* WRITE SECTORS */
        } else {
            ata_cmd_lba48(base, drive, lba, n, 0x34);   /* WRITE SECTORS EXT */
        }
        if (ata_wait_idle(base) != 0) return -1;
        for (uint32_t s = 0; s < n; s++) {
            if (ata_wait_drq(base) != 0) return -1;
            const uint8_t *p = buffer + s * 512;
            for (int i = 0; i < 256; i++) {
                uint16_t data = (uint16_t)p[i * 2] | ((uint16_t)p[i * 2 + 1] << 8);
                outw(base, data);
            }
        }
        /* 数据写完后等待驱动器真正落盘（BSY 清除） */
        if (ata_wait_idle(base) != 0) return -1;
        lba += n;
        buffer += n * 512;
        count -= n;
    }
    return 0;
}

/* ============ 块读缓存（64 扇区 LRU，写穿透失效） ============
 * 读命中直接拷贝免打盘；写成功后失效同 (drive,lba) 保证一致性。
 * 全部访问都在 blk_lock 临界区内，无需额外的锁。 */
#define BC_N 64
static struct {
    uint32_t drive, lba, tick;          /* tick=0 表示空项 */
    uint8_t data[512];
} bc_tab[BC_N];
static uint32_t bc_tick;

static int bc_find(uint8_t drive, uint32_t lba) {
    for (int i = 0; i < BC_N; i++)
        if (bc_tab[i].tick && bc_tab[i].drive == drive && bc_tab[i].lba == lba)
            return i;
    return -1;
}

static void bc_fill(uint8_t drive, uint32_t lba, const uint8_t *buf) {
    int v = 0;
    for (int i = 0; i < BC_N; i++) {
        if (bc_tab[i].tick == 0) { v = i; break; }
        if (bc_tab[i].tick < bc_tab[v].tick) v = i;   /* 最久未用者淘汰 */
    }
    bc_tab[v].drive = drive;
    bc_tab[v].lba = lba;
    bc_tab[v].tick = ++bc_tick;
    for (int j = 0; j < 512; j++) bc_tab[v].data[j] = buf[j];
}

static void bc_invalidate(uint8_t drive, uint32_t lba) {
    for (int j = 0; j < BC_N; j++)
        if (bc_tab[j].tick && bc_tab[j].drive == drive && bc_tab[j].lba == lba)
            bc_tab[j].tick = 0;
}

static int ata_read_sectors_unlocked(uint8_t drive, uint32_t lba, uint32_t count,
                                     uint8_t *buffer) {
    if (count == 0) return 0;
    if (drive >= NVME_DRIVE_BASE) {
        uint8_t ns = (uint8_t)(drive - NVME_DRIVE_BASE);
        for (uint32_t i = 0; i < count; i++)
            if (nvme_read_sector(ns, lba + i, buffer + i * 512) != 0) return -1;
        return 0;
    }
    if (drive >= USBMSC_DRIVE_BASE) {
        uint8_t u = (uint8_t)(drive - USBMSC_DRIVE_BASE);
        while (count > 0) {
            uint32_t n = (count > 32u) ? 32u : count;
            if (usbmsc_read_sectors(u, lba, n, buffer) != 0) return -1;
            lba += n; buffer += n * 512; count -= n;
        }
        return 0;
    }
    if (drive >= AHCI_DRIVE_BASE) {
        uint8_t p = (uint8_t)(drive - AHCI_DRIVE_BASE);
        if (p >= AHCI_MAX_PORTS) return -1;
        /* count 大于 ahci_submit 单命令上限时分段 */
        while (count > 0) {
            uint32_t n = (count > 128u) ? 128u : count;
            if (ahci_read_sectors(p, lba, n, buffer) != 0) return -1;
            lba += n; buffer += n * 512; count -= n;
        }
        return 0;
    }
    return pata_read_multi(drive, lba, count, buffer);
}

static int ata_write_sectors_unlocked(uint8_t drive, uint32_t lba, uint32_t count,
                                      const uint8_t *buffer) {
    if (count == 0) return 0;
    if (drive >= NVME_DRIVE_BASE) {
        uint8_t ns = (uint8_t)(drive - NVME_DRIVE_BASE);
        for (uint32_t i = 0; i < count; i++)
            if (nvme_write_sector(ns, lba + i, buffer + i * 512) != 0) return -1;
        return 0;
    }
    if (drive >= USBMSC_DRIVE_BASE) {
        uint8_t u = (uint8_t)(drive - USBMSC_DRIVE_BASE);
        while (count > 0) {
            uint32_t n = (count > 32u) ? 32u : count;
            if (usbmsc_write_sectors(u, lba, n, buffer) != 0) return -1;
            lba += n; buffer += n * 512; count -= n;
        }
        return 0;
    }
    if (drive >= AHCI_DRIVE_BASE) {
        uint8_t p = (uint8_t)(drive - AHCI_DRIVE_BASE);
        if (p >= AHCI_MAX_PORTS) return -1;
        while (count > 0) {
            uint32_t n = (count > 128u) ? 128u : count;
            if (ahci_write_sectors(p, lba, n, buffer) != 0) return -1;
            lba += n; buffer += n * 512; count -= n;
        }
        return 0;
    }
    return pata_write_multi(drive, lba, count, buffer);
}

int ata_read_sector(uint8_t drive, uint32_t lba, uint8_t *buffer) {
    return ata_read_sectors(drive, lba, 1, buffer);
}

int ata_write_sector(uint8_t drive, uint32_t lba, const uint8_t *buffer) {
    return ata_write_sectors(drive, lba, 1, buffer);
}

int ata_read_sectors(uint8_t drive, uint32_t lba, uint32_t count, uint8_t *buffer) {
    blk_lock();
    int rc = 0;
    uint32_t i = 0;
    while (i < count && rc == 0) {
        int h = bc_find(drive, lba + i);
        if (h >= 0) {
            const uint8_t *src = bc_tab[h].data;
            uint8_t *dst = buffer + i * 512;
            for (int j = 0; j < 512; j++) dst[j] = src[j];
            bc_tab[h].tick = ++bc_tick;
            i++;
            continue;
        }
        /* 连续未命中区间一次打盘，避免逐扇区请求 */
        uint32_t run = 1;
        while (i + run < count && bc_find(drive, lba + i + run) < 0) run++;
        rc = ata_read_sectors_unlocked(drive, lba + i, run, buffer + i * 512);
        if (rc == 0)
            for (uint32_t s = 0; s < run; s++)
                bc_fill(drive, lba + i + s, buffer + (i + s) * 512);
        i += run;
    }
    blk_unlock();
    return rc;
}

int ata_write_sectors(uint8_t drive, uint32_t lba, uint32_t count, const uint8_t *buffer) {
    blk_lock();
    int rc = ata_write_sectors_unlocked(drive, lba, count, buffer);
    if (rc == 0)
        for (uint32_t i = 0; i < count; i++)
            bc_invalidate(drive, lba + i);
    blk_unlock();
    return rc;
}

/* 零扇区批量写：内部 64 扇区全零缓冲分段写。
 * 格式化清零大段区域专用——调用方手里往往只有 512B 的零缓冲，
 * 直接把它当 count 扇区的缓冲传给 ata_write_sectors 会越界读栈。 */
void ata_write_zero_sectors(uint8_t drive, uint32_t lba, uint32_t count) {
    static uint8_t zero_buf[64 * 512];
    while (count > 0) {
        uint32_t n = (count > 64u) ? 64u : count;
        if (ata_write_sectors(drive, lba, n, zero_buf) != 0) return;
        lba += n;
        count -= n;
    }
}

/* ============ 探测输出 ============ */

static void print_dec(uint32_t num) {
    char buf[16];
    int len = 0;
    if (num == 0) {
        terminal_putchar('0');
        return;
    }
    while (num > 0) {
        buf[len++] = '0' + (num % 10);
        num /= 10;
    }
    while (len > 0) terminal_putchar(buf[--len]);
}

static void print_hex_byte(uint8_t val) {
    char hex[] = "0123456789ABCDEF";
    terminal_putchar(hex[val >> 4]);
    terminal_putchar(hex[val & 0x0F]);
}

void ata_init(void) {
    uint8_t mbr[512];
    static const char *bus_name[2] = { "primary", "secondary" };

    for (uint8_t d = 0; d < 4; d++) {
        terminal_writestring("ATA: ");
        terminal_writestring(bus_name[d >> 1]);
        terminal_writestring((d & 1) ? " slave" : " master");
        terminal_writestring(" (drive ");
        terminal_putchar('0' + d);
        terminal_writestring(")...\n");
        if (!ata_drive_present(d)) {
            terminal_writestring("  not present\n");
            continue;
        }
        if (ata_read_sector(d, 0, mbr) != 0) {
            terminal_writestring("  read error\n");
            continue;
        }
        if (mbr[510] == 0x55 && mbr[511] == 0xAA) {
            terminal_writestring("  OK, MBR signature valid\n");
            for (int i = 0; i < 4; i++) {
                uint8_t *entry = mbr + 446 + i * 16;
                uint8_t type = entry[4];
                if (type == 0) continue;
                uint32_t start_lba = *((uint32_t *)(entry + 8));
                uint32_t num_sectors = *((uint32_t *)(entry + 12));
                terminal_writestring("    Partition ");
                terminal_putchar('0' + i);
                terminal_writestring(": type=0x");
                print_hex_byte(type);
                terminal_writestring(", start=");
                print_dec(start_lba);
                terminal_writestring(", sectors=");
                print_dec(num_sectors);
                terminal_putchar('\n');
            }
        } else {
            terminal_writestring("  no valid MBR (superfloppy?)\n");
        }
    }
}
