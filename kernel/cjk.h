/* UTF-8 decoder and the linked GNU Unifont subset. */
#ifndef CJK_H
#define CJK_H

/* 用内核自己的 types.h（uint32_t=unsigned int）。不能引 <stdint.h>：
 * MinGW 的 stdint.h 在本项目配置下解析出的 uint32_t 与 types.h 的不同，
 * 混用会让同一函数在声明/定义处类型冲突（-Werror 直接红）。 */
#include "types.h"

/* 解码一个 UTF-8 序列（与 gfx.c 的 gfx_seq_len 同语义：非法首字节/截断
 * 序列按 1 字节走）。成功返回码点（>0）并写出实际占用字节数；失败返回 0。 */
uint32_t cjk_utf8_decode(const char *s, uint32_t n, uint32_t *adv);

/* Returns 32 bytes of bitmap rows, and sets width to 8 or 16 on success. */
const uint8_t *cjk_lookup(uint32_t cp,int *width);

/* Returns the number of pixels drawn, or zero if the font lacks that glyph. */
int cjk_draw_glyph(int x, int y, uint32_t cp, uint8_t fg, uint8_t bg);

#endif
