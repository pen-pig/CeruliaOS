/*
 * reg.c - 注册表式 kv 配置实现（/registry.db）
 *
 * 行式 "key=value"：set 读全文件进堆缓冲、改行、整文件重写（fs_create_file
 * 是 create-or-replace）。key [A-Za-z0-9._-] 1..63，value 可打印 1..255，
 * 文件上限 16KB（kmalloc 搬运）。value 无类型——kv 就是 kv，应用自己解释。
 */
#include "types.h"
#include "reg.h"
#include "fs.h"
#include "kmalloc.h"

#define REG_PATH     "/registry.db"
#define REG_MAX_FILE (16u * 1024u)
#define REG_MAX_KEY  63u
#define REG_MAX_VAL  255u

static uint32_t r_len(const char *s) {
    uint32_t n = 0;
    while (s[n]) n++;
    return n;
}

static int r_key_ok(const char *k) {
    uint32_t n = r_len(k);
    if (n == 0 || n > REG_MAX_KEY) return 0;
    for (uint32_t i = 0; i < n; i++) {
        char c = k[i];
        int ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                 (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-';
        if (!ok) return 0;
    }
    return 1;
}

static int r_val_ok(const char *v) {
    uint32_t n = r_len(v);
    if (n == 0 || n > REG_MAX_VAL) return 0;
    for (uint32_t i = 0; i < n; i++) {
        char c = v[i];
        if (c < 0x20 || c == 0x7F) return 0;   /* 不收控制字符/换行 */
    }
    return 1;
}

/* 读全文件进 kmalloc 缓冲；*out_len 回写字节数。0=成功 -1=读失败。
 * 文件不存在视为空表（len=0），首条 set 时创建。 */
static int r_load(uint8_t **out_buf, uint32_t *out_len) {
    uint32_t size;
    uint8_t d;
    *out_len = 0;
    if (fs_stat(REG_PATH, &size, &d) != 0) {
        *out_buf = 0;
        return 0;
    }
    if (size > REG_MAX_FILE) return -1;
    uint8_t *buf = (size > 0) ? (uint8_t *)kmalloc(size) : 0;
    if (size > 0 && buf == 0) return -1;
    if (size > 0 && fs_read_file(REG_PATH, buf, size) < 0) {
        if (buf) kfree(buf);
        return -1;
    }
    *out_buf = buf;
    *out_len = size;
    return 0;
}

/* 行内找 key："key=value"。返回 value 起始指针（行内），0=不是这个 key */
static const char *r_line_match(const uint8_t *line, uint32_t llen,
                                const char *key) {
    uint32_t kl = r_len(key);
    if (llen <= kl + 1) return 0;
    for (uint32_t i = 0; i < kl; i++)
        if ((char)line[i] != key[i]) return 0;
    if (line[kl] != '=') return 0;
    return (const char *)line + kl + 1;
}

int reg_get(const char *key, char *val, uint32_t valsz) {
    if (val == 0 || valsz == 0 || !r_key_ok(key)) return -1;
    uint8_t *buf;
    uint32_t len;
    if (r_load(&buf, &len) != 0) return -1;
    int found = 0;
    uint32_t i = 0;
    while (i < len) {
        uint32_t st = i;
        while (i < len && buf[i] != '\n') i++;
        uint32_t llen = i - st;
        const char *v = r_line_match(buf + st, llen, key);
        if (v != 0) {
            uint32_t vl = (uint32_t)((buf + st + llen) - (const uint8_t *)v);
            if (vl >= valsz) vl = valsz - 1;
            for (uint32_t j = 0; j < vl; j++) val[j] = v[j];
            val[vl] = 0;
            found = 1;
            break;
        }
        i++;   /* 跳过 \n */
    }
    if (buf) kfree(buf);
    return found ? 0 : -1;
}

int reg_set(const char *key, const char *val) {
    if (!r_key_ok(key) || !r_val_ok(val)) return -1;
    uint8_t *buf;
    uint32_t len;
    if (r_load(&buf, &len) != 0) return -1;

    /* 新文件 = 旧行全量拷贝（跳过本 key 的行）+ 新行；按字节组装 */
    uint32_t kl = r_len(key), vl = r_len(val);
    uint32_t need = len + kl + vl + 2;
    if (need > REG_MAX_FILE) {
        if (buf) kfree(buf);
        return -1;
    }
    uint8_t *nb = (uint8_t *)kmalloc(need);
    if (nb == 0) {
        if (buf) kfree(buf);
        return -1;
    }
    uint32_t w = 0;
    uint32_t i = 0;
    while (i < len) {
        uint32_t st = i;
        while (i < len && buf[i] != '\n') i++;
        uint32_t llen = i - st;
        if (r_line_match(buf + st, llen, key) == 0) {
            for (uint32_t j = 0; j < llen && w < need - 1; j++) nb[w++] = buf[st + j];
            if (w < need - 1) nb[w++] = '\n';
        }
        i++;
    }
    for (uint32_t j = 0; j < kl && w < need - 1; j++) nb[w++] = (uint8_t)key[j];
    if (w < need - 1) nb[w++] = '=';
    for (uint32_t j = 0; j < vl && w < need - 1; j++) nb[w++] = (uint8_t)val[j];
    if (w < need - 1) nb[w++] = '\n';
    nb[w] = 0;

    int rc = fs_create_file(REG_PATH, nb, w);
    kfree(nb);
    if (buf) kfree(buf);
    if (rc != 0) return (rc == -2) ? -2 : -3;
    return 0;
}

int reg_del(const char *key) {
    if (!r_key_ok(key)) return -1;
    uint8_t *buf;
    uint32_t len;
    if (r_load(&buf, &len) != 0) return -1;
    if (len == 0) return -1;   /* 文件不存在，必无此 key */

    uint8_t *nb = (uint8_t *)kmalloc(len);
    if (nb == 0) {
        kfree(buf);
        return -1;
    }
    uint32_t w = 0, i = 0, hit = 0;
    while (i < len) {
        uint32_t st = i;
        while (i < len && buf[i] != '\n') i++;
        uint32_t llen = i - st;
        if (r_line_match(buf + st, llen, key) != 0) {
            hit = 1;
        } else {
            for (uint32_t j = 0; j < llen && w < len; j++) nb[w++] = buf[st + j];
            if (w < len) nb[w++] = '\n';
        }
        i++;
    }
    kfree(buf);
    if (!hit) {
        kfree(nb);
        return -1;
    }
    int rc = (w == 0) ? fs_delete_file(REG_PATH)
                      : fs_create_file(REG_PATH, nb, w);
    kfree(nb);
    if (rc != 0) return (rc == -2) ? -2 : -3;
    return 0;
}

int reg_list(char *out, uint32_t outsz) {
    if (out == 0 || outsz == 0) return -1;
    out[0] = 0;
    uint8_t *buf;
    uint32_t len;
    if (r_load(&buf, &len) != 0) return -1;
    int n = 0;
    uint32_t w = 0;
    uint32_t i = 0;
    while (i < len) {
        uint32_t st = i;
        while (i < len && buf[i] != '\n') i++;
        uint32_t llen = i - st;
        /* 行内 "=" 之后是 value：整行原样输出（本身就是 key=value） */
        for (uint32_t j = 0; j < llen && w < outsz - 1; j++) out[w++] = (char)buf[st + j];
        if (llen > 0 && w < outsz - 1) { out[w++] = '\n'; n++; }
        i++;
    }
    if (buf) kfree(buf);
    out[w] = 0;
    return n;
}
