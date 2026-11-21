#ifndef ATA_H
#define ATA_H

#include "types.h"

int ata_read_sector(uint8_t drive, uint32_t lba, uint8_t *buffer);
int ata_write_sector(uint8_t drive, uint32_t lba, const uint8_t *buffer);
/* 多扇区批量读写（count*512B 连续缓冲，一次块层锁）：PATA/AHCI 原生
 * 多扇区命令，NVMe/USB-MSC 同锁内逐扇区。0<count，失败返回 -1。 */
int ata_read_sectors(uint8_t drive, uint32_t lba, uint32_t count, uint8_t *buffer);
int ata_write_sectors(uint8_t drive, uint32_t lba, uint32_t count, const uint8_t *buffer);
/* 零扇区批量写（内部缓冲，供格式化清零大段区域） */
void ata_write_zero_sectors(uint8_t drive, uint32_t lba, uint32_t count);
int ata_drive_present(uint8_t drive);
void ata_init(void);

/* 磁盘总扇区数（512B/扇区），ATA/AHCI/USB-MSC/NVMe 四条通路统一入口。
 * 拿不到容量（未上线 / IDENTIFY 失败 / ATAPI）返回 0 —— fail closed，
 * 调用方必须据此拒绝格式化，而不是按猜的大小写盘。 */
uint32_t ata_capacity(uint8_t drive);

#endif
