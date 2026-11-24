/* UTF-8 decoding and RAM-resident framebuffer glyphs. See tools/build_cjk.py. */
#include "cjk.h"
#include "gfx.h"

extern const uint8_t cjk_bitmap_start[], cjk_bitmap_end[];
#define CJK_RECORD 35u

uint32_t cjk_utf8_decode(const char *s, uint32_t n, uint32_t *adv) {
    const uint8_t *p = (const uint8_t *)s;
    if (n == 0) { if (adv) *adv = 0; return 0; }
    uint8_t b = p[0];
    if (b < 0x80) { if (adv) *adv = 1; return b; }
    /* 与 gfx_seq_len 同一套非法序列判定：非法首字节按 1 字节走 */
    if (b < 0xC2 || b >= 0xF5) { if (adv) *adv = 1; return 0; }
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

const uint8_t *cjk_lookup(uint32_t cp, int *width) {
    if (cp < 0xA0 || cp > 0xFFFF) return NULL;
    uint32_t lo=0, hi=((__UINTPTR_TYPE__)cjk_bitmap_end-(__UINTPTR_TYPE__)cjk_bitmap_start)/CJK_RECORD;
    while (lo < hi) {
        uint32_t mid = lo+(hi-lo)/2;
        const uint8_t *p=cjk_bitmap_start+mid*CJK_RECORD;
        uint32_t u = p[0] | ((uint32_t)p[1]<<8);
        if (u == cp) {
            *width=p[2];
            return p+3;
        }
        if (u < cp) lo = mid + 1; else hi = mid;
    }
    return NULL;
}

int cjk_draw_glyph(int x, int y, uint32_t cp, uint8_t fg, uint8_t bg) {
    int width;
    const uint8_t *bm = cjk_lookup(cp,&width);
    if (!bm) return 0;
    for (int row = 0; row < 16; row++) {
        for (int col = 0; col < width; col++) {
            int on = (bm[row * 2 + col / 8] & (0x80 >> (col % 8))) != 0;
            gfx_putpixel(x + col, y + row, on ? fg : bg);
        }
    }
    return width;
}
