/* cjk_font.h - GB2312 映射表（由 tools/gen_cjk_font.py 生成，勿手改）
 * unicode(codepoint) -> 点阵 slot，按 codepoint 升序，供二分查找。 */
#ifndef CJK_FONT_H
#define CJK_FONT_H

#include "types.h"

#define CJK_MAP_COUNT 7445u

typedef struct {
    uint16_t uni;   /* unicode 码点 */
    uint16_t slot;  /* cjk_bitmaps 下标（有效码位压缩序号，生成时算定） */
} cjk_map_ent_t;

extern const cjk_map_ent_t cjk_map[CJK_MAP_COUNT];
extern const uint8_t cjk_bitmaps[CJK_MAP_COUNT][32]; /* 压缩序号点阵 */

#endif
