/*
 * webview.c - Cerulia 浏览器渲染层（C 侧）
 *
 * 定位：**排版 + 绘制 + 交互**，不做解析。解析与断行在 Rust
 * （rust/ezos_rs/src/html.rs），理由见那里——纯计算、易错、要对拍，
 * 而 C 侧只负责"把排好版的行画到屏幕上"和"把点击坐标换算回链接"。
 *
 * 一条完整链路：
 *   http_fetch -> wv_page[] 原始字节
 *              -> html_parse_rs -> wv_node[] 节点 + wv_text[] 正文 + wv_str[] 字符串
 *              -> wv_layout()   -> wv_line[] 排好版的行（y/缩进/样式/链接）
 *              -> wv_draw_page() 按滚动偏移只画可见行
 *
 * 为什么中间一定要"排版成行"这一步，而不是画的时候现算：
 *   * 滚动条长度需要**先知道整页多高**，否则滚动条随滚动跳变；
 *   * 命中测试（点的是哪个链接）需要稳定坐标；
 *   * 每帧重排 = 每帧重排几百行，QEMU TCG 下直接掉帧。
 *
 * 内存纪律：任务内核栈只有 16KB，所有大缓冲一律 static（.bss，22MB 起，
 * 30MB 上限 —— 现在总量约 175KB，离上限很远）。
 */
#include "webview.h"
#include "gfx.h"
#include "http.h"
#include "rust_bridge.h"
#include "isr.h"
#include "keyboard.h"
/* 自检输出走串口。这两个在 shell_extra.c 里定义，一直没有公共头
 * （games.c 也是自己 extern 的）——这里跟着同一套做法，别为了自检
 * 去动 shell_extra.h 的依赖方向。 */
extern void ezos_console_putchar(char c);
extern void ezos_console_write(const char *s);

/* 与 Rust 的 HtmlNode 一一对应（rust_bridge.h 里有偏移断言） */
typedef struct {
    uint16_t text_off;
    uint16_t text_len;
    uint16_t href_off;
    uint8_t  kind;
    uint8_t  tag;
    uint8_t  style;
    uint8_t  level;
} wv_node_t;

/* 跨模块强转（这里把 wv_node_t* 当 html_node_t* 传给 Rust）必须钉死偏移：
 * Rust 侧的 #[repr(C)] 布局一改而 C 侧没跟着改，表现是"页面能打开但正文
 * 全是乱码"，现场几乎无从定位。 */
#define WV_OFF(f) __builtin_offsetof(wv_node_t, f)
_Static_assert(WV_OFF(text_off) == 0, "text_off @0");
_Static_assert(WV_OFF(text_len) == 2, "text_len @2");
_Static_assert(WV_OFF(href_off) == 4, "href_off @4");
_Static_assert(WV_OFF(kind)       == 6, "kind @6");
_Static_assert(WV_OFF(tag)        == 7, "tag @7");
_Static_assert(WV_OFF(style)      == 8, "style @8");
_Static_assert(WV_OFF(level)      == 9, "level @9");
/* size 必须是 10：3*u16 + 4*u8 = 10，正好是 2 的倍数，**没有尾部
 * padding**。有 padding 的话 Rust 侧按 10 步进会读错位（表现为随机节点
 * 类型），而 C 侧按 12 步进又会浪费内存 —— 两边必须同数。 */
_Static_assert(sizeof(wv_node_t) == 10, "wv_node_t must be 10B (Rust HtmlNode)");
_Static_assert(_Alignof(wv_node_t) == 2, "alignment");

/* ---------------- 容量（按真实网页估，不是拍脑袋） ---------------- */
#define WV_PAGE_MAX    (96u * 1024u)   /* 原始 HTML 字节 */
#define WV_NODES_MAX   3000u
#define WV_TEXT_MAX    (40u * 1024u)
#define WV_STR_MAX     (16u * 1024u)
#define WV_STACK_MAX   2048u
#define WV_LINES_MAX   1400u
#define WV_URL_MAX     256
#define WV_TITLE_MAX   80
#define WV_HIST_MAX    16

static uint8_t  wv_page[WV_PAGE_MAX]     __attribute__((section(".bss")));
static wv_node_t wv_node[WV_NODES_MAX]   __attribute__((section(".bss")));
static uint8_t  wv_text[WV_TEXT_MAX]     __attribute__((section(".bss")));
static uint8_t  wv_str[WV_STR_MAX]       __attribute__((section(".bss")));
static uint8_t  wv_stack[WV_STACK_MAX]   __attribute__((section(".bss")));
static uint32_t wv_starts[WV_LINES_MAX]  __attribute__((section(".bss")));
static wv_line_t wv_line[WV_LINES_MAX]   __attribute__((section(".bss")));

static uint32_t wv_starts_n;
static uint32_t wv_line_n;
static int32_t  wv_page_h;
static uint32_t wv_truncated;

static char wv_url[WV_URL_MAX]           __attribute__((section(".bss")));
static char wv_title[WV_TITLE_MAX]       __attribute__((section(".bss")));
static int32_t  wv_status;
static int32_t  wv_error;
static uint32_t wv_bytes;
static uint32_t wv_node_count;
static uint32_t wv_title_off;    /* str 竞技场偏移 + 1；0 = 没抓到 */
static uint32_t wv_title_len;

static char wv_hist_url[WV_HIST_MAX][WV_URL_MAX]   __attribute__((section(".bss")));
static char wv_hist_title[WV_HIST_MAX][WV_TITLE_MAX] __attribute__((section(".bss")));
static int wv_hist_n;
static int wv_hist_pos = -1;

int wv_scroll;
static int wv_bar_focus;
static int wv_bar_cursor;
static int wv_bar_scroll;

/* 内容区几何（浏览器窗口给出；wv_layout 用它算每行能放几个字） */
static int wv_view_w = WV_PAGE_W;
static int wv_view_h = WV_PAGE_H;

#define WV_LH 10      /* 行高：8px 字模 + 2px 行距 */
#define WV_MARGIN 6

/* ---------------- 小工具 ---------------- */

static int wv_len(const char *s) { int n = 0; while (s[n]) n++; return n; }

static int wv_cpy(char *dst, uint32_t cap, const char *src)
{
    uint32_t i = 0;
    if (cap == 0) return -1;
    while (src[i] && i + 1 < cap) { dst[i] = src[i]; i++; }
    dst[i] = 0;
    return (int)i;
}

static int wv_streq(const char *a, const char *b)
{
    int i = 0;
    while (a[i] && a[i] == b[i]) i++;
    return a[i] == b[i];
}

/* ---------------- 排版 ---------------- */

static void wv_emit(int y, int indent, uint8_t fs, uint8_t fx, uint8_t scale,
                    uint32_t off, uint32_t len, uint16_t href)
{
    if (wv_line_n >= WV_LINES_MAX) { wv_truncated = 1; return; }
    wv_line_t *L = &wv_line[wv_line_n++];
    L->y = (int16_t)y;
    L->indent = (uint8_t)indent;
    L->fs = fs;
    L->fx = fx;
    L->scale = scale;
    L->text_off = (uint16_t)off;
    L->text_len = (uint16_t)len;
    L->href = href;
}

/* 再给行记录带上"这段文字在竞技场里的绝对偏移"：排版时 utf8_wrap_rs
 * 返回的是**块内相对偏移**，直接存进text_off 的话，第二个 run 起的
 * 所有行都会按错的地址去画 —— 表现为"每一段正文都显示第一段的内容"
 * （more 显示成 click、mono 显示成 bold）。绘制与自检都按绝对偏移读，
 * 所以这里必须先把块基址加上。 */
static void wv_emit_abs(int y, int indent, uint8_t fs, uint8_t fx,
                        uint8_t scale, uint32_t base, uint32_t off,
                        uint32_t len, uint16_t href)
{
    if (wv_line_n >= WV_LINES_MAX) { wv_truncated = 1; return; }
    wv_line_t *L = &wv_line[wv_line_n++];
    L->y = (int16_t)y;
    L->indent = (uint8_t)indent;
    L->fs = fs;
    L->fx = fx;
    L->scale = scale;
    L->text_off = (uint16_t)(base + off);
    L->text_len = (uint16_t)len;
    L->href = href;
}

/*
 * 排一个文本块。src/len 指到某个竞技场（text 或 str），base 是 src 在
 * 该竞技场里的起始偏移（断行返回块内相对偏移，要加上它才是绝对偏移）。
 * 断行交给 utf8_wrap_rs —— 它保证不从 UTF-8 序列中间切开。
 */
static int wv_layout_run(int y, int indent, uint8_t fs, uint8_t fx,
                         uint8_t scale, const uint8_t *src, uint32_t len,
                         uint16_t href, uint32_t base)
{
    if (len == 0) return y;
    int avail = wv_view_w - indent - WV_MARGIN;
    if (avail < 8 * 4) avail = 8 * 4;
    int max_chars = avail / (8 * scale);
    if (max_chars < 4) max_chars = 4;
    if ((uint32_t)max_chars > WV_LINES_MAX - wv_starts_n)
        max_chars = WV_LINES_MAX - wv_starts_n;
    if (max_chars <= 0) return y;

    uint32_t nl = utf8_wrap_rs(src, len, (uint32_t)max_chars,
                               wv_starts + wv_starts_n,
                               WV_LINES_MAX - wv_starts_n);
    if (nl == 0) return y;
    for (uint32_t k = 0; k < nl; k++) {
        uint32_t s = wv_starts[wv_starts_n + k];
        uint32_t e = (k + 1 < nl) ? wv_starts[wv_starts_n + k + 1] : len;
        /* 裁掉行尾空格：断行时为对齐把空格归到了行末，画出来右边会不齐 */
        while (e > s && src[s + e - 1] == ' ') e--;
        if (e <= s) continue;
        wv_emit_abs(y, indent, fs, fx, scale, base, s, e - s, href);
        y += WV_LH * scale;
    }
    wv_starts_n += nl;
    return y;
}

static int wv_block_gap(uint8_t tag, uint8_t level)
{
    if (tag >= WV_T_H1 && tag <= WV_T_H6) return (level <= 2) ? 8 : 5;
    if (tag == WV_T_LI || tag == WV_T_DD || tag == WV_T_DT) return 1;
    if (tag == WV_T_HR) return 4;
    return 3;
}

/* 标题放大要**跨节点传递**：<h1> 是个块节点（不带正文），正文在紧随
 * 其后的文本节点里。所以块节点记下"下一段用几倍字号"，文本节点消费掉它。
 *
 * 早先写的是 `uint8_t scale = (N->style & WV_FS_BOLD) ? 1 : 1;` ——
 * 三目两边都是 1，等于恒为 1，标题从来没被放大过，而 WV_FX_SCALE2
 * 这个位定义了却从没有代码去写它。绘制侧其实一直支持 scale（行高按
 * L->scale 乘），只是布局侧从没给出过 2。
 *
 * 只给 h1/h2 放大：8x8 字模放大两倍是 16x16，在 420px 宽的页面里一行
 * 只剩 26 个字，再大的标题在这个分辨率下已经没法读了。 */
static int wv_next_scale;

static void wv_layout(void)
{
    wv_line_n = 0;
    wv_starts_n = 0;
    wv_truncated = 0;
    wv_next_scale = 1;

    /* 页面标题排最上面（Rust 已把它单独抓出来放 str 竞技场） */
    wv_title[0] = 0;
    int y = WV_MARGIN;
    if (wv_title_len > 0 && wv_title_off > 0) {
        uint32_t o = wv_title_off - 1;
        uint32_t n = wv_title_len;
        if (o + n > WV_STR_MAX) n = WV_STR_MAX - o;
        y = wv_layout_run(y, WV_MARGIN, WV_FS_BOLD, WV_FX_TITLE, 1,
                          wv_str + o, n, 0, o);
        y += 4;
        wv_emit(y, WV_MARGIN, 0, WV_FX_MUTED, 1, 0, 0, 0);
        wv_line[wv_line_n - 1].fs = 0;      /* 分隔线：text_len=0 时画横线 */
        wv_line[wv_line_n - 1].scale = 0;   /* scale==0 是"分隔线"标记 */
        y += 8;
    }

    int indent = 0;
    for (uint32_t i = 0; i < wv_node_count && wv_line_n < WV_LINES_MAX; i++) {
        const wv_node_t *N = &wv_node[i];
        if (N->kind == WV_N_TEXT) {
            uint32_t o = N->text_off, n = N->text_len;
            if (n == 0 || o + n > WV_TEXT_MAX) continue;
            uint8_t scale = (uint8_t)wv_next_scale;
            wv_next_scale = 1;          /* 只放大紧随标题的那一段 */
            y = wv_layout_run(y, indent, N->style,
                              N->href_off ? WV_FX_LINK : 0, scale,
                              wv_text + o, n, N->href_off, o);
            continue;
        }
        if (N->kind == WV_N_BR) { y += WV_LH; continue; }
        if (N->kind == WV_N_HR) {
            y += 4;
            if (wv_line_n < WV_LINES_MAX) {
                wv_emit(y, WV_MARGIN, 0, WV_FX_MUTED, 0, 0, 0, 0);
            }
            y += 8;
            continue;
        }
        if (N->kind == WV_N_IMG) {
            uint32_t o = N->text_off, n = N->text_len;
            if (n == 0 || o + n > WV_STR_MAX) continue;
            /* alt 画成 "[alt]"：先排 alt 本身，前面加一个 '[' 用单独的一行
             * 太啰嗦，直接在绘制时补画括号（fx 里有 ALT 位就不必占行）。 */
            y = wv_layout_run(y, indent + 8, 0,
                              WV_FX_MUTED | WV_FX_IN_STR, 1,
                              wv_str + o, n, 0, o);
            continue;
        }
        if (N->kind == WV_N_BLOCK) {
            uint8_t tag = N->tag, level = N->level;
            int gap = wv_block_gap(tag, level);
            if (y > WV_MARGIN + 8) y += gap;
            if (tag == WV_T_LI) {
                indent = 10 + (level > 2 ? 2 : level) * 6;
                /* 项目符号：3x3 实心方块（8x8 字模里没有 • 字形，
                 * 硬去 VGA ROM 找一个也不可靠） */
                if (wv_line_n < WV_LINES_MAX)
                    wv_emit(y, indent - 7, 0, WV_FX_MUTED, 3, 0, 0, 0);
                y += WV_LH;
            } else if (tag == WV_T_UL || tag == WV_T_OL) {
                indent = 10 + (level > 2 ? 2 : level) * 6;
            } else if (tag >= WV_T_H1 && tag <= WV_T_H6) {
                indent = 0;
                wv_next_scale = (level <= 2) ? 2 : 1;
            } else if (tag == WV_T_BLOCKQUOTE) {
                indent = 12;
            } else {
                indent = 0;
            }
            /* 标题本体：h1/h2 放大两倍，其余保持 8x8 */
            if (tag >= WV_T_H1 && tag <= WV_T_H6) {
                if (wv_line_n < WV_LINES_MAX) {
                    /* 标题样式由紧随其后的文本节点继承不了（style 是每个
                     * 文本节点自己的），所以在这里显式插一条"标记行"：
                     * 渲染时它不带文字，只用来加上下留白 —— 见下面
                     * wv_emit(y, indent, fs, WV_FX_TITLE...) 的用法。 */
                    wv_emit(y, 0, 0, WV_FX_TITLE, 0, 0, 0, 0);
                }
            }
            continue;
        }
    }

    y += WV_MARGIN;
    if (wv_truncated) {
        /* 明确告诉用户"没显示完"，别让人以为这就是全文 */
        static const char msg[] = "... [page truncated: Cerulia limit]";
        wv_emit(y, WV_MARGIN, 0, WV_FX_MUTED | WV_FX_LITERAL, 1,
                (uint32_t)(unsigned long)msg, (uint32_t)(sizeof(msg) - 1), 0);
        y += WV_LH + 2;
    }
    wv_page_h = y;
}

/* ---------------- 加载 ---------------- */

/*
 * 相对 URL -> 绝对（跟随 <a href> 时用）。
 *
 * 刻意只做三件事：绝对 URL 原样保留、锚点/javascript 丢弃、其余按
 * "当前 URL 替换最后一段"或"保留到 host 结束"拼。**不做** RFC 3986 的
 * 完整实现（`..` 归一化、query 覆盖等）——那些在真实网页里极少触发，
 * 而写错一半反而更糟；真遇到了会得到一个抓不到的 URL，用户看得到报错。
 */
static void wv_absolutize(const char *href, char *out, uint32_t cap)
{
    uint32_t i = 0;
    out[0] = 0;
    if (!href || !href[0]) return;

    /* 已是绝对 URL */
    if ((href[0] == 'h' || href[0] == 'H') &&
        (href[1] == 't' || href[1] == 'T') &&
        (href[2] == 't' || href[2] == 'T') &&
        (href[3] == 'p' || href[3] == 'P')) {
        wv_cpy(out, cap, href);
        return;
    }
    /* 锚点 / javascript: / mailto: —— 内核浏览器一个都不支持 */
    if (href[0] == '#' ||
        (href[0] == 'j' || href[0] == 'J') ) return;
    if (href[0] == 'm' || href[0] == 'M' || href[0] == 't' || href[0] == 'T') {
        /* mailto: / tel: / data: 之类一律不支持，明确不猜 */
        int colon = 0;
        for (i = 0; href[i] && i < 12; i++) if (href[i] == ':') colon = 1;
        if (colon) return;
    }

    /* 当前 URL 的 scheme://host 结束位置 */
    uint32_t ulen = (uint32_t)wv_len(wv_url);
    uint32_t base = 0;
    for (i = 0; wv_url[i]; i++) {
        if (wv_url[i] == '/' && i + 2 < ulen && wv_url[i + 1] == '/') {
            base = i + 3;
            break;
        }
    }
    if (base == 0) {
        /* 当前 URL 不是 http://... 形式（首屏还没加载）：无从拼接 */
        if (href[0] == '/') wv_cpy(out, cap, href);
        return;
    }
    uint32_t host_end = base;
    while (wv_url[host_end] && wv_url[host_end] != '/') host_end++;

    if (href[0] == '/') {
        uint32_t k = 0;
        while (k < host_end && k + 1 < cap) { out[k] = wv_url[k]; k++; }
        uint32_t q = 0;
        while (href[q] && k + q + 1 < cap) { out[k + q] = href[q]; q++; }
        out[k + q] = 0;
        return;
    }
    /* 相对路径：把当前 URL 最后一段（最后一个 '/' 之后）换掉 */
    uint32_t slash = 0;
    for (i = host_end; wv_url[i]; i++) if (wv_url[i] == '/') slash = i;
    uint32_t k = 0;
    while (k < slash && k + 1 < cap) { out[k] = wv_url[k]; k++; }
    uint32_t q = 0;
    while (href[q] && k + q + 1 < cap) { out[k + q] = href[q]; q++; }
    out[k + q] = 0;
}

/* 抓一个 URL 并排版。成功返回 0。 */
static int wv_load(const char *url)
{
    if (!url || !url[0]) return -1;

    wv_cpy(wv_url, sizeof(wv_url), url);
    wv_scroll = 0;
    wv_bar_cursor = wv_len(wv_url);
    wv_bar_scroll = 0;
    wv_title[0] = 0;
    wv_error = 0;
    wv_truncated = 0;
    wv_node_count = 0;
    wv_title_off = 0;
    wv_title_len = 0;
    wv_line_n = 0;
    wv_page_h = 20;
    wv_status = 0;
    wv_bytes = 0;

    http_result_t r;
    int rc = http_fetch(url, wv_page, WV_PAGE_MAX, 8000, &r);
    wv_status = r.status;
    wv_bytes = r.body_len;
    if (rc != 0) { wv_error = rc; return rc; }
    if (r.body_len == 0) { wv_page_h = 20; return 0; }

    uint32_t stats[12];
    int nodes = html_parse_rs(wv_page, r.body_len,
                              (html_node_t *)wv_node, WV_NODES_MAX,
                              wv_text, WV_TEXT_MAX, wv_str, WV_STR_MAX,
                              wv_stack, WV_STACK_MAX, stats);
    if (nodes < 0) { wv_error = -5; return -5; }

    wv_node_count = stats[0];
    if (stats[3]) wv_truncated = 1;
    wv_title_len = stats[5];
    wv_title_off = stats[4];

    wv_layout();
    return 0;
}

/* 从内存解析并排版（不联网）。htmltest 用。
 *
 * 与 wv_load 的区别只在数据来源：抓取那步换成调用方给的字节。**必须
 * 共用 wv_layout**，否则"自检看到的排版"和"浏览器看到的排版"就成了
 * 两条路径，自检全绿也说明不了浏览器是对的 —— 这正是这个命令存在的
 * 意义，所以不许在这里另写一份排版。 */
int wv_parse_mem(const void *html, uint32_t len, const char *label)
{
    if (!html) return -1;

    wv_cpy(wv_url, sizeof(wv_url), label ? label : "(memory)");
    wv_scroll = 0;
    wv_bar_cursor = wv_len(wv_url);
    wv_bar_scroll = 0;
    wv_title[0] = 0;
    wv_error = 0;
    wv_truncated = 0;
    wv_node_count = 0;
    wv_title_off = 0;
    wv_title_len = 0;
    wv_line_n = 0;
    wv_page_h = 20;
    wv_status = 0;
    wv_bytes = len;

    if (len == 0 || len > WV_PAGE_MAX) { wv_page_h = 20; return 0; }

    uint32_t stats[12];
    int nodes = html_parse_rs((const uint8_t *)html, len,
                              (html_node_t *)wv_node, WV_NODES_MAX,
                              wv_text, WV_TEXT_MAX, wv_str, WV_STR_MAX,
                              wv_stack, WV_STACK_MAX, stats);
    if (nodes < 0) { wv_error = -5; return -5; }

    wv_node_count = stats[0];
    if (stats[3]) wv_truncated = 1;
    wv_title_len = stats[5];
    wv_title_off = stats[4];

    wv_layout();
    return 0;
}

/* ---------------- 自检输出（htmltest 命令） ---------------- */

uint16_t wv_line_count(void) { return (uint16_t)wv_line_n; }
uint32_t wv_nodes_parsed(void) { return wv_node_count; }
/* 访问器叫 wv_is_truncated 而不是 wv_truncated：后者是文件内部的
 * static 变量名，同名会遮蔽掉它，`if (wv_truncated)` 就会变成
 * "取函数地址恒为真"—— -Werror=address 正好会抓住，但报错信息
 * 指向的是使用点而不是根因，容易查错地方。 */
uint32_t wv_is_truncated(void) { return wv_truncated; }
const wv_line_t *wv_line_at(uint16_t i) { return &wv_line[i]; }

/* 行的正文在哪块竞技场：靠 WV_FX_IN_STR 判别，不是靠猜。
 * 正文在 wv_text[]，<img alt> / href / title 在 wv_str[]。两者都是
 * 不含终止符的裸字节，返回的指针**只在 wv_line 有效期间**有意义。 */
const char *wv_line_text(const wv_line_t *ln)
{
    const uint8_t *base = (ln->fx & WV_FX_IN_STR) ? wv_str : wv_text;
    return (const char *)(base + ln->text_off);
}

static void wv_dump_num(uint32_t v)
{
    char b[12];
    int n = 0;
    if (v == 0) { ezos_console_write("0"); return; }
    while (v && n < 11) { b[n++] = (char)('0' + (v % 10)); v /= 10; }
    for (int i = n - 1; i >= 0; i--) ezos_console_putchar(b[i]);
}

void wv_dump(const char *label)
{
    ezos_console_write("htmltest ");
    ezos_console_write(label ? label : "(none)");
    ezos_console_write("\n");
    ezos_console_write("  title=");
    ezos_console_write(wv_title_len ? wv_title : "");
    ezos_console_write("\n");
    ezos_console_write("  bytes=");
    wv_dump_num(wv_bytes);
    ezos_console_write(" nodes=");
    wv_dump_num(wv_node_count);
    ezos_console_write(" lines=");
    wv_dump_num(wv_line_n);
    ezos_console_write(" pageh=");
    wv_dump_num((uint32_t)wv_page_h);
    ezos_console_write(wv_truncated ? " truncated\n" : "\n");

    /* 每行一个字段。分行的目的是让 E2E 用"取包含某串的行"就能断言，
     * 不必猜列位置 —— 猜列位置的测试在字段一改就假红。 */
    for (uint32_t i = 0; i < wv_line_n; i++) {
        const wv_line_t *L = &wv_line[i];
        ezos_console_write("  L");
        wv_dump_num(i);
        ezos_console_write(" y=");
        wv_dump_num((uint32_t)L->y);
        ezos_console_write(" ind=");
        wv_dump_num(L->indent);
        ezos_console_write(" fs=");
        wv_dump_num(L->fs);
        ezos_console_write(" fx=");
        wv_dump_num(L->fx);
        ezos_console_write(" sc=");
        wv_dump_num(L->scale);
        ezos_console_write(" href=");
        wv_dump_num(L->href);
        ezos_console_write(" |");
        const uint8_t *base = (L->fx & WV_FX_IN_STR) ? wv_str : wv_text;
        for (uint32_t k = 0; k < L->text_len; k++)
            ezos_console_putchar((char)base[L->text_off + k]);
        ezos_console_write("\n");
    }
    ezos_console_write("htmltest end\n");
}

/* ---------------- 内建 HTML 样本（htmltest sample <n>） ----------------
 *
 * 挑这 6 条的理由：每一条都钉住一类**错了也不会立刻看出来**的规则。
 * 真实网页错在这几处，屏幕上仍然是"一篇看着正常的文章"。
 *
 *  1 实体：&amp; &lt; &#65; &hellip; 展开，&unknown; 原样保留
 *  2 标题分级 + 段落分段（<h1> 放大两倍，level 带进排版）
 *  3 链接：href 进 str 竞技场，正文分两段（点击区不能横跨两段）
 *  4 嵌套样式：<b>/<i>/<code> 的样式要能一层层脱掉
 *  5 script/style 里的假标签不能进正文
 *  6 UTF-8 中文 + <br>：断行绝不能把码点切坏（ref_html 里 utf8 与 br
 *    是两条，这里合成一条，两条规则一趟验完）
 *
 * 样本 1-5 与 tests/ref_html.py 的 CASES **逐字节相同**（由
 * tools/check_html.py 里的 sync_samples 钉住，不一致就红）。样本 6 是
 * 两条规则的合并，对应 ref_html 的 'utf8' + 'br'。
 */
static const char *const HTML_SAMPLES[] = {
    /* 1 */ "<p>a&amp;b &lt;c&gt; &#65; &#x42; &nbsp;&hellip; &unknown; &amp</p>",
    /* 2 */ "<h1>Title</h1><p>One</p><h2>Sub</h2><p>Two</p>",
    /* 3 */ "<a href=\"/a.html\">click</a> and <a href=\"b.html\">more</a>",
    /* 4 */ "<p><b>bold</b> and <i>ital</i> and <code>mono</code></p>",
    /* 5 */ "<p>a</p><script>var x = \"<p>fake</p>\";</script><p>b</p>",
    /* 6 */ "<p>\xe4\xbd\xa0\xe5\xa5\xbd\xe4\xb8\x96\xe7\x95\x8c<br>line2</p>",
};
#define HTML_SAMPLE_N ((int)(sizeof(HTML_SAMPLES) / sizeof(HTML_SAMPLES[0])))

int html_sample_count(void) { return HTML_SAMPLE_N; }

const char *html_sample(int n)
{
    if (n < 1 || n > HTML_SAMPLE_N) return 0;
    return HTML_SAMPLES[n - 1];
}

uint32_t wv_strlen(const char *s)
{
    uint32_t n = 0;
    if (!s) return 0;
    while (s[n]) n++;
    return n;
}

static void wv_hist_add(const char *url, const char *title)
{
    if (wv_hist_n > 0 && wv_streq(wv_hist_url[wv_hist_n - 1], url)) {
        wv_cpy(wv_hist_title[wv_hist_n - 1], WV_TITLE_MAX, title);
        wv_hist_pos = wv_hist_n - 1;
        return;
    }
    if (wv_hist_n >= WV_HIST_MAX) {
        /* 满了丢掉最老的一半（整体前移）。不用环形索引 —— 环形会让
         * "当前在哪一格"的判断分散到两处，历史上就是这么写错过的。 */
        int half = WV_HIST_MAX / 2;
        for (int i = 0; i < half; i++) {
            wv_cpy(wv_hist_url[i], WV_URL_MAX, wv_hist_url[half + i]);
            wv_cpy(wv_hist_title[i], WV_TITLE_MAX, wv_hist_title[half + i]);
        }
        wv_hist_n = half;
    }
    wv_cpy(wv_hist_url[wv_hist_n], WV_URL_MAX, url);
    wv_cpy(wv_hist_title[wv_hist_n], WV_TITLE_MAX, title);
    wv_hist_n++;
    wv_hist_pos = wv_hist_n - 1;
}

void wv_init(void)
{
    wv_hist_n = 0;
    wv_hist_pos = -1;
    wv_url[0] = 0;
    wv_title[0] = 0;
    wv_error = 0;
    wv_scroll = 0;
    wv_line_n = 0;
    wv_page_h = 20;
    wv_bar_focus = 0;
}

int wv_go(const char *url)
{
    int rc = wv_load(url);
    if (rc == 0) wv_hist_add(wv_url, wv_title);
    return rc;
}

int wv_can_back(void)    { return wv_hist_pos > 0; }
int wv_can_forward(void) { return wv_hist_pos >= 0 && wv_hist_pos < wv_hist_n - 1; }

int wv_back(void)
{
    if (!wv_can_back()) return -1;
    wv_hist_pos--;
    return wv_load(wv_hist_url[wv_hist_pos]);
}

int wv_forward(void)
{
    if (!wv_can_forward()) return -1;
    wv_hist_pos++;
    return wv_load(wv_hist_url[wv_hist_pos]);
}

int wv_reload(void)
{
    if (!wv_url[0]) return -1;
    return wv_go(wv_url);
}

/* 跟随一个链接（点中后调用）。href 是 wv_str 竞技场里的偏移 + 1。 */
int wv_follow(uint16_t href)
{
    if (href == 0) return -1;
    char target[WV_URL_MAX];
    wv_absolutize((const char *)wv_str + (href - 1), target, sizeof(target));
    if (!target[0]) return -1;      /* 锚点/javascript：内核不支持，忽略 */
    return wv_go(target);
}

const char *wv_current_url(void)   { return wv_url; }
const char *wv_current_title(void) { return wv_title; }
uint32_t    wv_byte_count(void)    { return wv_bytes; }
int32_t     wv_http_status(void)   { return wv_status; }

const char *wv_err_text(int rc)
{
    switch (rc) {
    case -1: return "bad arguments";
    case -2: return "DNS lookup failed";
    case -3: return "connection failed";
    case -4: return "send failed";
    case -5: return "bad response";
    case -6: return "protocol error";
    case -7: return "https not supported (no TLS)";
    case -8: return "too many redirects";
    default: return "load failed";
    }
}

/* ---------------- 绘制 ---------------- */

static uint8_t wv_fg_for(uint8_t fs, uint8_t fx)
{
    if (fx & WV_FX_LINK)  return WV_C_LINK;
    if (fx & WV_FX_TITLE) return WV_C_TITLE;
    if (fx & WV_FX_MUTED) return WV_C_MUTED;
    (void)fs;
    return WV_C_FG;
}

/* 每行起点（二分）在 wv_draw_page 里内联，避免多一层调用 */
static const wv_line_t *wv_line_ptr(uint32_t i) { return &wv_line[i]; }

void wv_draw_page(int ox, int oy, int iw, int ih)
{
    gfx_set_clip(ox, oy, iw, ih);
    gfx_fill_rect(ox, oy, iw, ih, WV_C_BG);

    /* 内容区尺寸变了就重排：排版是按"每行能放几个字"算的，窗口拉伸后
     * 不重排的话右边会空一大块（或者反过来，文字被裁掉）。重排只在尺寸
     * 真的变了时发生，不是每帧。 */
    if (iw > 8 && ih > 8 && (iw != wv_view_w || ih != wv_view_h)) {
        wv_view_w = iw;
        wv_view_h = ih;
        if (wv_node_count > 0) wv_layout();
    }

    if (wv_error != 0) {
        gfx_draw_text(ox + 8, oy + 8, wv_err_text(wv_error), WV_C_ERR, WV_C_BG);
        const char *h = "F1: edit URL   F5: reload";
        gfx_draw_text(ox + 8, oy + 8 + WV_LH + 2, h, WV_C_MUTED, WV_C_BG);
        gfx_set_clip(-1, -1, 0, 0);
        return;
    }
    if (wv_line_n == 0) {
        const char *h = "type a URL and press Enter";
        gfx_draw_text(ox + 8, oy + 8, h, WV_C_MUTED, WV_C_BG);
        gfx_set_clip(-1, -1, 0, 0);
        return;
    }

    /* 二分找第一条可见行：1400 行逐行扫在 TCG 下白烧 */
    uint32_t lo = 0, hi = wv_line_n;
    while (lo < hi) {
        uint32_t mid = (lo + hi) / 2;
        const wv_line_t *L = wv_line_ptr(mid);
        int lh = L->scale ? L->scale : 1;
        if (L->y + WV_LH * lh <= wv_scroll) lo = mid + 1;
        else hi = mid;
    }

    for (uint32_t i = lo; i < wv_line_n; i++) {
        const wv_line_t *L = wv_line_ptr(i);
        int y = oy + L->y - wv_scroll;
        if (y > oy + ih) break;
        int x = ox + L->indent;

        /* scale == 0：分隔线 */
        if (L->scale == 0) {
            gfx_fill_rect(x, y, iw - 2 * WV_MARGIN, 1, WV_C_MUTED);
            continue;
        }
        /* scale == 3：项目符号（3x3 方块） */
        if (L->scale == 3) {
            gfx_fill_rect(x, y + 2, 3, 3, WV_C_FG);
            continue;
        }
        /* 空行（标题的上下留白标记） */
        if (L->text_len == 0 && !(L->fx & WV_FX_LITERAL)) continue;

        const uint8_t *src = (L->fx & WV_FX_IN_STR) ? wv_str : wv_text;
        if (L->fx & WV_FX_LITERAL) src = (const uint8_t *)(unsigned long)L->text_off;

        uint8_t fg = wv_fg_for(L->fs, L->fx);
        uint8_t bg = WV_C_BG;
        if (L->fx & WV_FX_IN_STR) fg = WV_C_MUTED;   /* alt 恒为次要色 */

        if (L->fx & WV_FX_TITLE) fg = WV_C_TITLE;

        int w = gfx_draw_text_utf8(x, y, (const char *)src, L->text_len, fg, bg);
        /* 下划线：<u>/<a> 与标题 */
        if ((L->fs & WV_FS_UNDER) || (L->fx & (WV_FX_LINK | WV_FX_TITLE))) {
            int uw = w - x;
            if (x + uw <= ox + iw) gfx_fill_rect(x, y + 8, uw, 1, fg);
        }
        if (L->fs & WV_FS_STRIKE) {
            int uw = w - x;
            if (x + uw <= ox + iw) gfx_fill_rect(x, y + 4, uw, 1, fg);
        }
    }

    /* 滚动条 */
    if (wv_page_h > ih) {
        int tx = ox + iw - 5;
        gfx_fill_rect(tx, oy, 5, ih, WV_C_TRACK);
        int th = (ih * ih) / wv_page_h;
        if (th < 14) th = 14;
        if (th > ih) th = ih;
        int maxs = wv_page_h - ih;
        int ty = maxs > 0 ? (wv_scroll * (ih - th)) / maxs : 0;
        if (ty < 0) ty = 0;
        if (ty + th > ih) ty = ih - th;
        gfx_fill_rect(tx, oy + ty, 5, th, WV_C_THUMB);
    }
    gfx_set_clip(-1, -1, 0, 0);
}

void wv_draw_bar(int ox, int oy, int iw, int h,
                 int *bar_x, int *bar_y, int *bar_w, int *bar_h)
{
    int my = oy + (h - 8) / 2;
    /* 后退 / 前进（不可用时画灰的） */
    gfx_draw_text(ox + 4, my, "<", wv_can_back() ? WV_C_FG : WV_C_OFF, WV_C_TOOLBAR);
    gfx_draw_text(ox + 15, my, ">", wv_can_forward() ? WV_C_FG : WV_C_OFF, WV_C_TOOLBAR);

    int ax = ox + 27;
    int aw = iw - 27 - 46;
    if (aw < 30) aw = 30;
    int ay = oy + 3;
    int ah = h - 6;
    uint8_t bar_bg = wv_bar_focus ? WV_C_BAR_FOCUS : WV_C_BAR;
    gfx_fill_rect(ax, ay, aw, ah, bar_bg);
    gfx_draw_rect(ax, ay, aw, ah, WV_C_BORDER);

    if (bar_x) *bar_x = ax;
    if (bar_y) *bar_y = ay;
    if (bar_w) *bar_w = aw;
    if (bar_h) *bar_h = ah;

    int max_chars = (aw - 6) / 8;
    if (max_chars < 1) max_chars = 1;
    int total = wv_len(wv_url);
    if (wv_bar_cursor - wv_bar_scroll > max_chars - 1)
        wv_bar_scroll = wv_bar_cursor - max_chars + 1;
    if (wv_bar_scroll < 0) wv_bar_scroll = 0;
    if (total <= max_chars) wv_bar_scroll = 0;

    int from = wv_bar_scroll;
    int cnt = total - from;
    if (cnt > max_chars) cnt = max_chars;
    if (cnt > 0) {
        gfx_draw_text_utf8(ax + 3 - wv_bar_scroll * 8, ay + (ah - 8) / 2,
                           wv_url + from, (uint32_t)cnt, WV_C_FG, bar_bg);
    }
    /* 光标：闪烁方块（8x8 字模里没有 _ ） */
    int cx = ax + 3 + (wv_bar_cursor - wv_bar_scroll) * 8;
    if (cx < ax + 2) cx = ax + 2;
    if (cx > ax + aw - 3) cx = ax + aw - 3;
    if (wv_bar_focus && ((g_pit_ticks / 30) & 1) == 0)
        gfx_fill_rect(cx, ay + (ah - 8) / 2, 1, 8, WV_C_FG);

    /* 状态 */
    const char *st;
    if (wv_error) st = "ERR";
    else if (wv_truncated) st = "CUT";
    else if (!wv_line_n) st = "";
    else st = "OK";
    gfx_draw_text(ox + iw - 22, my, st, WV_C_MUTED, WV_C_TOOLBAR);
}

/* ---------------- 交互 ---------------- */

int wv_bar_has_focus(void) { return wv_bar_focus; }

void wv_bar_set_focus(int f)
{
    wv_bar_focus = f;
    if (f) { wv_bar_cursor = wv_len(wv_url); wv_bar_scroll = 0; }
}

void wv_bar_key(int key)
{
    if (!wv_bar_focus) return;
    int n = wv_len(wv_url);
    if (key >= 32 && key < 127) {
        if (n + 1 < (int)sizeof(wv_url)) {
            for (int i = n; i > wv_bar_cursor; i--) wv_url[i] = wv_url[i - 1];
            wv_url[wv_bar_cursor] = (char)key;
            wv_url[n + 1] = 0;
            wv_bar_cursor++;
        }
        return;
    }
    if (key == 8 || key == 127) {
        if (wv_bar_cursor > 0) {
            for (int i = wv_bar_cursor - 1; i < n; i++) wv_url[i] = wv_url[i + 1];
            wv_bar_cursor--;
        }
        return;
    }
    if (key == KEY_LEFT  && wv_bar_cursor > 0)  { wv_bar_cursor--; return; }
    if (key == KEY_RIGHT && wv_bar_cursor < n)   { wv_bar_cursor++; return; }
    if (key == KEY_UP    || key == KEY_PGUP) { wv_scroll_by(-40); return; }
    if (key == KEY_DOWN  || key == KEY_PGDN) { wv_scroll_by(40);  return; }
    if (key == 27) { wv_bar_focus = 0; return; }        /* ESC */
    if (key == '\n' || key == '\r') {
        wv_bar_focus = 0;
        char u[WV_URL_MAX];
        wv_cpy(u, sizeof(u), wv_url);
        if (!u[0]) return;
        /* 裸域名补 http://（用户最常忘的就是这个） */
        int scheme = 0;
        for (int i = 0; u[i]; i++) {
            if (u[i] == ':' && u[i + 1] == '/' && u[i + 2] == '/') { scheme = 1; break; }
            if (u[i] == '/' || u[i] == ' ') break;
        }
        char full[WV_URL_MAX];
        if (!scheme) {
            if ((size_t)wv_len(u) + 8 < sizeof(full)) {
                full[0] = 'h'; full[1] = 't'; full[2] = 't'; full[3] = 'p';
                full[4] = ':'; full[5] = '/'; full[6] = '/'; full[7] = 0;
                wv_cpy(full + 7, sizeof(full) - 7, u);
            } else {
                return;   /* 太长：留在编辑状态，不截出一个坏 URL 去抓 */
            }
        } else {
            wv_cpy(full, sizeof(full), u);
        }
        wv_go(full);
        return;
    }
}

void wv_page_key(int key)
{
    switch (key) {
    case KEY_UP:   wv_scroll_by(-WV_LH); break;
    case KEY_DOWN: wv_scroll_by(WV_LH);  break;
    case KEY_PGUP: wv_scroll_by(-60);     break;
    case KEY_PGDN: wv_scroll_by(60);      break;
    case KEY_HOME: wv_scroll_to_top();    break;
    case KEY_END:  wv_scroll_by(1000000); break;
    case KEY_F1:   wv_bar_set_focus(1);   break;
    case KEY_F2:   wv_back();             break;
    case KEY_F3:   wv_forward();          break;
    case KEY_F5:   wv_reload();           break;
    case '\n': case '\r': wv_bar_set_focus(1); break;
    default: break;
    }
}

void wv_scroll_by(int dy)
{
    int ih = wv_view_h > 0 ? wv_view_h : WV_PAGE_H;
    int m = wv_page_h - ih;
    if (m < 0) m = 0;
    int v = wv_scroll + dy;
    if (v < 0) v = 0;
    if (v > m) v = m;
    wv_scroll = v;
}

void wv_scroll_to_top(void) { wv_scroll = 0; }
int  wv_scroll_get(void) { return wv_scroll; }

/* Dino 页是内置页面（ezos://dino），不走网络也不经过解析器。
 * 判定放在这里而不是 gfxwin.c，是为了让"哪些 URL 是内置页面"只有一处
 * 定义 —— 之前浏览器和 Dino 各判一次，加第三个内置页时必漏。 */
int wv_is_dino_url(void)
{
    static const char kDino[] = "ezos://dino";
    int i = 0;
    for (; i < (int)sizeof(kDino) - 1; i++)
        if (!wv_url[i] || wv_url[i] != kDino[i]) return 0;
    return wv_url[i] == 0;
}

uint16_t wv_hit_link(int px, int py)
{
    if (!wv_line_n) return 0;
    if (py < 0 || py >= wv_page_h) return 0;
    for (uint32_t i = 0; i < wv_line_n; i++) {
        const wv_line_t *L = &wv_line[i];
        if (!L->href) continue;
        if (L->text_len == 0) continue;
        int lh = WV_LH * (L->scale ? L->scale : 1);
        if (py < L->y || py >= L->y + lh) continue;
        const uint8_t *src = (L->fx & WV_FX_IN_STR) ? wv_str : wv_text;
        int w = gfx_text_width_utf8((const char *)src, L->text_len) * (L->scale ? L->scale : 1);
        if (px >= L->indent && px < L->indent + w) return L->href;
    }
    return 0;
}
