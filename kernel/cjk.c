/* cjk.c - GB2312 16x16 点阵渲染（配合 gfx.c 的 UTF-8 文本路径）
 *
 * 数据在 cjk_font.c（生成物）：cjk_map[] 按 codepoint 升序（二分查找），
 * cjk_bitmaps[7445][32] 按 GB2312 区位序。
 *
 * 纪律：7445*32=238KB 点阵 + 7445*6=44KB 映射都在 .rodata——内核镜像
 * 547KB -> 约 830KB，仍在 linker.ld 的 3MB window ASSERT 内，且不占 BSS。
 * 字库不进镜像曾是可选项（放 sysvol 启动加载），但"系统卷不依赖磁盘挂载"
 * 是本项目的卖点之一（format 之后 /system 仍可读），中文渲染属于同类
 * 基础设施，跟着进镜像。
 */
#include "cjk.h"
#include "cjk_font.h"
#include "gfx.h"

uint32_t cjk_utf8_decode(const char *s, uint32_t n, uint32_t *adv) {
    const uint8_t *p = (const uint8_t *)s;
    if (n == 0) { if (adv) *adv = 0; return 0; }
    uint8_t b = p[0];
    if (b < 0x80) { if (adv) *adv = 1; return b; }
    /* 与 gfx_seq_len 同一套非法序列判定：非法首字节按 1 字节走 */
    if (b == 0xC0 || b == 0xC1 || b >= 0xF5) { if (adv) *adv = 1; return 0; }
    uint32_t l = (b & 0xE0) == 0xC0 ? 2u : (b & 0xF0) == 0xE0 ? 3u : 4u;
    if (l > n) { if (adv) *adv = 1; return 0; }
    for (uint32_t k = 1; k < l; k++)
        if ((p[k] & 0xC0) != 0x80) { if (adv) *adv = 1; return 0; }
    uint32_t cp = b & (0xFF >> (l + 1));
    for (uint32_t k = 1; k < l; k++) cp = (cp << 6) | (p[k] & 0x3F);
    /* 过长编码/代理区/超范围都当非法（与 Rust utf8 侧一致，拒绝而非夹取） */
    static const uint32_t minv[5] = {0, 0, 0x80, 0x800, 0x10000};
    if (cp < minv[l] || cp > 0x10FFFF ||
        (cp >= 0xD800 && cp <= 0xDFFF)) { if (adv) *adv = 1; return 0; }
    if (adv) *adv = l;
    return cp;
}

const uint8_t *cjk_lookup(uint32_t cp) {
    if (cp < 0x80) return NULL;
    /* 二分：cjk_map 按 uni 升序；slot 由生成器算定（有效码位压缩序号），
     * 内核不做任何区位换算——公式曾经把 733 个空位算错，高区汉字越界。 */
    int lo = 0, hi = (int)CJK_MAP_COUNT - 1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        uint16_t u = cjk_map[mid].uni;
        if (u == cp) {
            uint32_t slot = cjk_map[mid].slot;
            if (slot >= CJK_MAP_COUNT) return NULL;
            const uint8_t *bm = cjk_bitmaps[slot];
            /* 全 0 = 该 slot 无字形（生成时字体缺字） */
            for (int i = 0; i < 32; i++) if (bm[i]) return bm;
            return NULL;
        }
        if (u < cp) lo = mid + 1; else hi = mid - 1;
    }
    return NULL;
}

int cjk_draw_glyph(int x, int y, uint32_t cp, uint8_t fg, uint8_t bg) {
    const uint8_t *bm = cjk_lookup(cp);
    if (!bm) return 0;
    for (int row = 0; row < 16; row++) {
        for (int col = 0; col < 16; col++) {
            int on = (bm[row * 2 + col / 8] & (0x80 >> (col % 8))) != 0;
            gfx_putpixel(x + col, y + row, on ? fg : bg);
        }
    }
    return 1;
}

int cjk_glyph_width(void) { return 16; }
