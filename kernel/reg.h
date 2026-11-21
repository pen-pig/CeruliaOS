/*
 * reg.h - 注册表式 kv 配置（/registry.db）
 *
 * 单文件行式存储："key=value\n"。reg get/set/del/list 走 fs_* 统一层。
 * spec 原定 /system/registry——/system 是只读 sysvol blob 不可写，落卷根。
 */
#ifndef REG_H
#define REG_H

#include "types.h"

/* 取值：0=成功（写回 val）；-1=key 不存在/未挂载 */
int reg_get(const char *key, char *val, uint32_t valsz);

/* 设值（create-or-replace 整文件重写）；0=成功 -1=参数/容量 -2=权限 -3=卷不可写 */
int reg_set(const char *key, const char *val);

/* 删 key；0=成功 -1=不存在 -2=权限 -3=卷不可写 */
int reg_del(const char *key);

/* 列表：把 "key=value\n" 拼进 out（总量上限 outsz）；返回条目数 */
int reg_list(char *out, uint32_t outsz);

#endif
