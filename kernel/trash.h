/*
 * trash.h - 回收站（/$RECYCLE.BIN）
 *
 * rm/GUI 删除统一通道：文件移入当前卷 /$RECYCLE.BIN，原名+原路径记在
 * <槽>.meta，内容存 <槽>.dat。restore 按槽号还原，purge 清空。
 */
#ifndef TRASH_H
#define TRASH_H

#include "types.h"

/* 回收站目录（当前数据卷根下） */
#define TRASH_DIR "/$RECYCLE.BIN"

/* 移入回收站：0=成功返回槽号(>=1)；-1=失败 -2=权限 -3=文件过大 -4=目录 */
int trash_put(const char *path);

/* 槽号 -> 原路径（写回 out，含原名）；0=成功 -1=槽不存在 */
int trash_get_orig(uint32_t slot, char *out, uint32_t outsz);

/* 槽号 -> 文件大小（0=槽不存在） */
uint32_t trash_get_size(uint32_t slot);

/* 还原到原路径；0=成功 -1=槽不存在 -2=权限 -3=还原目标创建失败 */
int trash_restore(uint32_t slot);

/* 清空回收站，返回清掉的条目数；-1=失败 */
int trash_purge(void);

#endif
