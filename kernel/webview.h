/*
 * webview.h - EZOS 浏览器渲染层
 *
 * 分工（为什么这么切）：
 *   http.c      HTTP/1.1 传输（字节）
 *   Rust        HTML 解析 + UTF-8 断行（结构）
 *   webview.c   排版 + 绘制 + 交互（像素）—— 布局/绘制分离的关键是
 *               **先排版再画**：滚动条长度与点击命中都需要"整页已知"，
 *               每帧现算既跳变又掉帧。
 */
#ifndef EZ_WEBVIEW_H
#define EZ_WEBVIEW_H

#include "types.h"

/* ---- 页面区几何（浏览器窗口内容区；窗口更大时按内容区算） ---- */
#define WV_PAGE_W   420
#define WV_PAGE_H   240

/* ---- 调色板索引（8bpp DAC；与 gfxwin 的 0xF0..0xF9 段错开） ---- */
#define WV_C_BG        0x01   /* 页面底色（近白）    */
#define WV_C_FG        0x00   /* 正文（近黑）        */
#define WV_C_MUTED     0x08   /* 次要文字 / 分隔线   */
#define WV_C_LINK      0x02   /* 链接（蓝）          */
#define WV_C_TITLE     0x04   /* 标题（深蓝）        */
#define WV_C_ERR       0x0C   /* 错误（红）          */
#define WV_C_TOOLBAR   0x07   /* 工具栏底（浅灰）    */
#define WV_C_BAR       0x0F   /* 地址栏底（白）      */
#define WV_C_BAR_FOCUS 0x1F   /* 地址栏底（聚焦）    */
#define WV_C_BORDER    0x08   /* 描边                */
#define WV_C_TRACK     0x07   /* 滚动槽              */
#define WV_C_THUMB     0x08   /* 滚动滑块            */
#define WV_C_OFF       0x08   /* 不可用按钮          */

/* ---- 行样式：两个字节，位互不重叠 ----
 * `fs` 直接来自 Rust 的 S_*（解析器给的），`fx` 是渲染层自己加的。
 * 早先合成一个字节时把 S_MONO 和"可点链接"挤到同一位，结果等宽代码段
 * 里的链接点不中 —— 两位分开就没这类串扰。 */
#define WV_FS_BOLD     0x01
#define WV_FS_ITALIC   0x02
#define WV_FS_UNDER    0x04
#define WV_FS_MONO     0x08
#define WV_FS_PRE      0x10
#define WV_FS_STRIKE   0x20

#define WV_FX_SCALE2   0x01   /* 放大两倍（h1/h2）      */
#define WV_FX_MUTED    0x02   /* 次要文字              */
#define WV_FX_LINK     0x04   /* 可点链接              */
#define WV_FX_IN_STR   0x08   /* 文本在 str 竞技场     */
#define WV_FX_LITERAL  0x10   /* text_off 是字面量地址 */
#define WV_FX_TITLE    0x20   /* 页面标题              */

/* ---- 节点种类（与 Rust 的 NODE_* 一致） ---- */
#define WV_N_TEXT   0
#define WV_N_BLOCK  1
#define WV_N_BR     2
#define WV_N_HR     3
#define WV_N_IMG    4

/* ---- 标签 id（与 Rust 的 T_* 一致；只列渲染用到的） ---- */
#define WV_T_OTHER      0
#define WV_T_P          1
#define WV_T_DIV        2
#define WV_T_H1         4
#define WV_T_H2         5
#define WV_T_H3         6
#define WV_T_H4         7
#define WV_T_H5         8
#define WV_T_H6         9
#define WV_T_UL         10
#define WV_T_OL         11
#define WV_T_LI         12
#define WV_T_DT         14
#define WV_T_DD         15
#define WV_T_BLOCKQUOTE 20
#define WV_T_PRE        21
#define WV_T_HR         22
#define WV_T_CENTER     23

/* 排版后的一行。12 字节 x 1400 行 = 16.8KB，static 放 .bss。 */
typedef struct {
    int16_t  y;         /* 相对页面顶的像素 */
    uint8_t  indent;
    uint8_t  fs;        /* 来自 Rust 的 S_*  */
    uint8_t  fx;        /* 渲染层扩展位     */
    uint8_t  scale;
    uint16_t text_off;
    uint16_t text_len;
    uint16_t href;      /* 0 = 非链接；否则 = str 竞技场偏移 + 1 */
} wv_line_t;

/* ---- 生命周期 ---- */
void wv_init(void);

/* 抓 URL 并排版。成功返回 0；失败返回 http_fetch 的错误码。 */
int  wv_go(const char *url);
/* 从内存解析并排版（不联网）。htmltest 命令用这条：E2E 要能对**本地
 * 文件**跑解析，网络一挂就分不清"页面没排出来"和"网不通"。 */
int  wv_parse_mem(const void *html, uint32_t len, const char *label);
int  wv_back(void);
int  wv_forward(void);
int  wv_reload(void);
int  wv_can_back(void);
int  wv_can_forward(void);
const char *wv_current_url(void);
const char *wv_current_title(void);
uint32_t    wv_byte_count(void);
int32_t     wv_http_status(void);
const char *wv_err_text(int rc);

/* ---- 绘制（浏览器窗口调用） ---- */
void wv_draw_page(int ox, int oy, int iw, int ih);
/* 画地址栏。bar_x/y/w/h 回传地址栏矩形，供点击判断。 */
void wv_draw_bar(int ox, int oy, int iw, int h,
                 int *bar_x, int *bar_y, int *bar_w, int *bar_h);

/* ---- 交互 ---- */
void     wv_bar_set_focus(int f);
int      wv_bar_has_focus(void);
void     wv_bar_key(int key);
void     wv_page_key(int key);
void     wv_scroll_by(int dy);
void     wv_scroll_to_top(void);
uint16_t wv_hit_link(int px, int py);   /* 命中返回 href 偏移，否则 0 */
int  wv_scroll_get(void);                    /* 当前滚动偏移 */
int  wv_is_dino_url(void);                   /* 当前 URL 是不是 ezos://dino */
int  wv_follow(uint16_t href);          /* 跟随链接（href 取自 wv_hit_link） */

/* ---- 自检（htmltest 命令用；输出全部走串口） ----
 * 把排版结果按稳定格式打出来，让 E2E 能逐行断言："标题是不是抓到了"、
 * "正文有没有被 &amp; 展开"、"断行有没有切坏 UTF-8"。格式故意做成
 * 一行一个字段，方便测试用"包含"取行，不必猜列位置。 */
void wv_dump(const char *label);
uint16_t wv_line_count(void);
uint32_t wv_nodes_parsed(void);   /* 解析出的节点数 */
const wv_line_t *wv_line_at(uint16_t i);
const char *wv_line_text(const wv_line_t *ln);   /* NUL 结尾，指向内部缓冲 */
uint32_t wv_is_truncated(void);   /* 正文/行数被截断（竞技场或行表满了） */

/* 内建 HTML 样本（htmltest sample <n>，n = 1..6）；返回 NULL 表示越界。
 *
 * 样本住在内核里而不是由测试脚本生成，理由是**送不进去**：QMP sendkey
 * 打不出 '<' '>' 这类字符（未映射的键名会被静默丢弃），所以 E2E 没法
 * 用键盘把 HTML 敲进 guest 再解析。样本必须和解析器在同一个二进制里。
 *
 * 编号与 tests/ref_html.py 的 CASES 逐条对应，改一处必须改另一处。 */
const char *html_sample(int n);
int         html_sample_count(void);
uint32_t    wv_strlen(const char *s);

#endif
