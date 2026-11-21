/*
 * trash.c - 回收站实现（/$RECYCLE.BIN）
 *
 * 结构：每个被删文件占一个槽位 n（从 1 递增找空位）：
 *   /$RECYCLE.BIN/n.dat   文件内容
 *   /$RECYCLE.BIN/n.meta  原绝对路径（含原名）
 * 全部走 fs_* 统一层，FAT/exFAT/ext4 等可写卷通用；只读卷/系统卷天然被拒。
 * 槽位上限 256、单文件上限 256KB（走 kmalloc 搬运）。
 */
#include "types.h"
#include "trash.h"
#include "fs.h"
#include "kmalloc.h"

#define TRASH_MAX_SLOT 256u
#define TRASH_MAX_SIZE (256u * 1024u)

/* n -> "/$RECYCLE.BIN/n.dat" / "/$RECYCLE.BIN/n.meta" */
static void trash_dat_path(uint32_t n, char *out, uint32_t outsz) {
    uint32_t i = 0;
    const char *d = TRASH_DIR;
    while (*d && i < outsz - 1) out[i++] = *d++;
    if (i < outsz - 1) out[i++] = '/';
    char num[12];
    uint32_t k = 0;
    if (n == 0) num[k++] = '0';
    while (n && k < sizeof(num)) { num[k++] = (char)('0' + n % 10); n /= 10; }
    while (k && i < outsz - 1) out[i++] = num[--k];
    const char *ext = ".dat";
    while (*ext && i < outsz - 1) out[i++] = *ext++;
    out[i] = 0;
}

static void trash_meta_path(uint32_t n, char *out, uint32_t outsz) {
    trash_dat_path(n, out, outsz);
    uint32_t i = 0;
    while (out[i]) i++;
    if (i >= 4) out[i - 4] = 0;   /* 去掉 ".dat" */
    const char *ext = ".meta";
    while (*ext && i < outsz - 1) out[i++] = *ext++;
    out[i] = 0;
}

static uint32_t t_len(const char *s) {
    uint32_t n = 0;
    while (s[n]) n++;
    return n;
}

int trash_put(const char *path) {
    uint32_t size = 0;
    uint8_t is_dir = 0;
    char full[256];
    if (fs_resolve_path(path, full, sizeof(full)) != 0) return -1;
    if (fs_stat(full, &size, &is_dir) != 0) return -1;
    if (is_dir) return -4;
    if (size > TRASH_MAX_SIZE) return -3;

    /* 建回收站目录（已存在则忽略）；ext4 卷上放开权限让任意用户能 rm */
    fs_mkdir(TRASH_DIR);
    fs_chmod(TRASH_DIR, 0777);   /* 非 ext4 卷返回 -3，忽略 */

    char datp[300], metap[300];
    uint32_t slot = 0;
    for (uint32_t n = 1; n <= TRASH_MAX_SLOT; n++) {
        trash_dat_path(n, datp, sizeof(datp));
        uint32_t sz;
        uint8_t d;
        if (fs_stat(datp, &sz, &d) != 0) { slot = n; break; }
    }
    if (slot == 0) return -1;    /* 满了 */

    uint8_t *buf = 0;
    if (size > 0) {
        buf = (uint8_t *)kmalloc(size);
        if (buf == 0) return -1;
        if (fs_read_file(full, buf, size) < 0) {
            kfree(buf);
            return -1;
        }
    }
    if (fs_create_file(datp, buf, size) != 0) {
        if (buf) kfree(buf);
        return -1;
    }
    if (buf) kfree(buf);

    trash_meta_path(slot, metap, sizeof(metap));
    if (fs_create_file(metap, (const uint8_t *)full, t_len(full)) != 0) {
        fs_delete_file(datp);
        return -1;
    }
    /* 内容与元数据都落了才删原件；删失败则回滚，等于没删 */
    if (fs_delete_file(full) != 0) {
        fs_delete_file(datp);
        fs_delete_file(metap);
        return -1;
    }
    return (int)slot;
}

int trash_get_orig(uint32_t slot, char *out, uint32_t outsz) {
    if (slot == 0 || slot > TRASH_MAX_SLOT || out == 0 || outsz == 0) return -1;
    char metap[300];
    trash_meta_path(slot, metap, sizeof(metap));
    uint32_t sz;
    uint8_t d;
    if (fs_stat(metap, &sz, &d) != 0) return -1;
    if (sz >= outsz) sz = outsz - 1;
    if (sz > 0 && fs_read_file(metap, (uint8_t *)out, sz) < 0) return -1;
    out[sz] = 0;
    return 0;
}

uint32_t trash_get_size(uint32_t slot) {
    if (slot == 0 || slot > TRASH_MAX_SLOT) return 0;
    char datp[300];
    trash_dat_path(slot, datp, sizeof(datp));
    uint32_t sz;
    uint8_t d;
    if (fs_stat(datp, &sz, &d) != 0) return 0;
    return sz;
}

int trash_restore(uint32_t slot) {
    char orig[256];
    if (trash_get_orig(slot, orig, sizeof(orig)) != 0) return -1;
    char datp[300], metap[300];
    trash_dat_path(slot, datp, sizeof(datp));
    trash_meta_path(slot, metap, sizeof(metap));
    uint32_t size;
    uint8_t d;
    if (fs_stat(datp, &size, &d) != 0) return -1;

    uint8_t *buf = 0;
    if (size > 0) {
        buf = (uint8_t *)kmalloc(size);
        if (buf == 0) return -1;
        if (fs_read_file(datp, buf, size) < 0) {
            kfree(buf);
            return -1;
        }
    }
    int rc = fs_create_file(orig, buf, size);   /* create-or-replace */
    if (buf) kfree(buf);
    if (rc != 0) return (rc == -2) ? -2 : -3;
    fs_delete_file(datp);
    fs_delete_file(metap);
    return 0;
}

int trash_purge(void) {
    int n = 0;
    for (uint32_t i = 1; i <= TRASH_MAX_SLOT; i++) {
        char datp[300], metap[300];
        trash_dat_path(i, datp, sizeof(datp));
        trash_meta_path(i, metap, sizeof(metap));
        uint32_t sz;
        uint8_t d;
        if (fs_stat(datp, &sz, &d) != 0) continue;
        if (fs_delete_file(datp) == 0) {
            fs_delete_file(metap);
            n++;
        } else {
            return -1;
        }
    }
    return n;
}
