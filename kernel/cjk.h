/* cjk.h - 中文（GB2312 16x16 点阵）渲染支持
 *
 * 由来：GUI/网页里的非 ASCII 文本原来一律画成豆腐块（见 gfx.c
 * gfx_draw_tofu 的注释）——中文网页整页空白，用户无法区分"没加载"和
 * "加载了但没字"。这里补上真正的汉字渲染：
 *   UTF-8 码点 --(cjk_map 二分)--> GB2312 区位 --> cjk_bitmaps 32B 点阵
 * 字库与映射表由 tools/gen_cjk_font.py 从 Windows 字体生成（生成物即
 * cjk_font.c/.h，进内核 .rodata；不在启动时读盘，format 后依然可用）。
 */
#ifndef CJK_H
#define CJK_H

/* 用内核自己的 types.h（uint32_t=unsigned int）。不能引 <stdint.h>：
 * MinGW 的 stdint.h 在本项目配置下解析出的 uint32_t 与 types.h 的不同，
 * 混用会让同一函数在声明/定义处类型冲突（-Werror 直接红）。 */
#include "types.h"

/* 解码一个 UTF-8 序列（与 gfx.c 的 gfx_seq_len 同语义：非法首字节/截断
 * 序列按 1 字节走）。成功返回码点（>0）并写出实际占用字节数；失败返回 0。 */
uint32_t cjk_utf8_decode(const char *s, uint32_t n, uint32_t *adv);

/* codepoint -> 16x16 点阵（32B，行优先、每字节高比特先行）。
 * GB2312 没有该字返回 NULL。 */
const uint8_t *cjk_lookup(uint32_t cp);

/* 画一个汉字到 (x,y)。返回 1 = 画出；0 = 无字形（调用方自行画豆腐块）。 */
int cjk_draw_glyph(int x, int y, uint32_t cp, uint8_t fg, uint8_t bg);

/* 一个汉字占的像素宽（16）。ASCII 仍是 8，见 gfx_draw_text_utf8。 */
int cjk_glyph_width(void);

#endif
