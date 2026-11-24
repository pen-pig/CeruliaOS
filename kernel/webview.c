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
#include "task.h"
#include "pmm.h"
#include "webimage.h"
#include "webcss.h"
#include "websession.h"
#include "auth.h"
#include "webipc.h"
#include "exec.h"
#include "rtc.h"
#include "kmalloc.h"
#include "mem.h"
#include "net.h"
#include "irqflags.h"
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
#define WV_PAGE_MAX    (256u * 1024u)  /* search results often exceed 100 KiB */
#define WV_NODES_MAX   3000u
#define WV_TEXT_MAX    (40u * 1024u)
#define WV_STR_MAX     (48u * 1024u) /* URL offsets must still fit uint16_t */
#define WV_STACK_MAX   2048u
#define WV_LINES_MAX   1400u
#define WV_URL_MAX     HTTP_URL_MAX
#define WV_TITLE_MAX   80
#define WV_HIST_MAX    16

static uint8_t  wv_page[WV_PAGE_MAX]     __attribute__((section(".bss")));
static uint8_t  wv_decoded[WV_PAGE_MAX];
static wv_node_t wv_node[WV_NODES_MAX]   __attribute__((section(".bss")));
static uint8_t  wv_text[WV_TEXT_MAX]     __attribute__((section(".bss")));
static uint8_t  wv_str[WV_STR_MAX]       __attribute__((section(".bss")));
static uint8_t  wv_stack[WV_STACK_MAX]   __attribute__((section(".bss")));
static wv_line_t wv_line[WV_LINES_MAX]   __attribute__((section(".bss")));

static uint32_t wv_line_n;
static int wv_tallest;
static int32_t  wv_page_h;
static uint32_t wv_truncated;

static char wv_url[WV_URL_MAX]           __attribute__((section(".bss")));
static char wv_title[WV_TITLE_MAX]       __attribute__((section(".bss")));
static int32_t  wv_status;
static int32_t  wv_error;
static uint32_t wv_bytes;
static uint32_t wv_node_count;
static int wv_anchor_y[WV_NODES_MAX];
static uint32_t wv_laid_out;
static uint32_t wv_title_off;    /* str 竞技场偏移 + 1；0 = 没抓到 */
static uint32_t wv_title_len;

static char wv_hist_url[WV_HIST_MAX][WV_URL_MAX]   __attribute__((section(".bss")));
static char wv_hist_title[WV_HIST_MAX][WV_TITLE_MAX] __attribute__((section(".bss")));
static int wv_hist_n;
static int wv_hist_pos = -1;

int wv_scroll;
static int wv_bar_focus;
static int wv_bar_cursor;
static int wv_bar_selected;
static int wv_bar_scroll;
/* The desktop owns all display and history state. The worker only touches
 * its immutable URL and the unpublished HTTP result/body. */
static char job_url[WV_URL_MAX], queued_url[WV_URL_MAX];
static int job_state, queued, loading, record_history;
static uint32_t generation, job_generation;
static http_result_t job_result;
static int job_rc;
/* URI history holds only the destination URL, never these form bytes. */
static uint8_t queued_post[1024],job_post[1024];
static unsigned queued_post_len,job_post_len;
static int queued_cookies,job_cookies;
static uint32_t job_session;
static void form_reset(void);
typedef struct script_source {struct script_source *next;char url[];} script_source;
static script_source *script_head,*script_tail,*job_scripts;
static int script_job;
static unsigned js_done,js_error,job_js_done,job_js_error;
static char *js_bytes,*job_js_bytes;
static unsigned js_pages,job_js_pages,job_js_used;
static uint16_t dom_offsets[WV_NODES_MAX],dom_ends[WV_NODES_MAX];
static uint16_t dom_lengths[WV_NODES_MAX];
static int same_site(const char *,const char *);
/* Only the desktop changes cache entries. The worker owns job_bitmap until
 * its release-store: an old download can never write into a new document. */
#define WV_RASTER_NODE 6
#define WV_RASTER_LINE 4
typedef struct {const char *ref; web_bitmap bmp; uint8_t requested;} picture;
static picture single_picture,*pictures;
static uint16_t picture_node[WV_NODES_MAX]; /* slot + 1; duplicates share storage */
static unsigned picture_count,picture_pages;
static int image_scan;
static int image_job;
static int style_job, style_blocked, styles_pending;
static int reader_mode;
static int backgrounds_allowed;
static unsigned text_zoom=100;
static unsigned style_document_pages,job_style_pages;
static uint8_t *style_document,*job_style;
static char image_base[WV_URL_MAX];
static web_bitmap job_bitmap;
void *web_image_pages(unsigned n){return (void*)pmm_alloc_pages(n);}
void web_image_release(void *p,unsigned n){pmm_free_pages((uint32_t)p,n);}
static void clear_images(void){
    for(unsigned i=0;i<picture_count;i++)web_free_image(&pictures[i].bmp);
    if(picture_pages)web_image_release(pictures,picture_pages);
    pictures=0;picture_count=picture_pages=0;image_scan=0;
    for(unsigned i=0;i<WV_NODES_MAX;i++)picture_node[i]=0;
}
static void clear_style(void){
    css_clear();
    if(style_document_pages)pmm_free_pages((uint32_t)style_document,style_document_pages);
    style_document=0;style_document_pages=0;styles_pending=0;style_blocked=0;backgrounds_allowed=0;
}
static void release_scripts(script_source *s){
    while(s){script_source *next=s->next;kfree(s);s=next;}
}
static void clear_js(void){
    release_scripts(script_head);script_head=script_tail=0;
    if(js_pages)web_image_release(js_bytes,js_pages);
    js_bytes=0;js_pages=0;js_done=js_error=0;
    memset(dom_ends,0,sizeof(dom_ends));
}
void wv_script_register(const uint8_t *ref,unsigned n){
    if(!n||n>=WV_URL_MAX)return;
    for(unsigned i=0;i<n;i++)if(ref[i]<=32||ref[i]=='&'||ref[i]=='\\')return;
    script_source *s=kmalloc(sizeof(*s)+n+1);
    if(!s)return;
    s->next=0;memcpy(s->url,ref,n);s->url[n]=0;
    if(script_tail)script_tail->next=s;else script_head=s;
    script_tail=s;
}
unsigned wv_scripts_done(void){return js_done;}
unsigned wv_scripts_error(void){return js_error;}
static int image_mime(const char *s){
    const char *names[]={"image/png","image/jpeg"};
    for(unsigned i=0;i<2;i++){
        const char *p=names[i],*q=s;
        while(*p && *p==(*q|32)){p++;q++;}
        if(!*p && (!*q || *q==';' || *q==' '))return 1;
    }
    return 0;
}
static void wv_hist_add(const char *, const char *);
static int image_cancelled(void){return __atomic_load_n(&generation,__ATOMIC_RELAXED)!=job_generation;}
static int live_dom_text(const uint8_t *id,uint32_t n,uint8_t *out){
    unsigned first,end,len=0;
    if(!css_dom_range(id,n,&first,&end)||first>=end||end>wv_node_count)return -1;
    for(unsigned i=first;i<end;i++){
        const wv_node_t *node=wv_node+i;
        unsigned off=node->text_off,size=node->text_len;
        if(node->kind!=WV_N_TEXT||!size||off+size>WV_TEXT_MAX)continue;
        if(out)memcpy(out+len,wv_text+off,size);
        len+=size;
    }
    return (int)len;
}
static int js_mime(const char *type){
    const char *types[]={"text/javascript","application/javascript","application/ecmascript","text/ecmascript"};
    for(unsigned i=0;i<sizeof(types)/sizeof(*types);i++){
        const char *p=type,*m=types[i];while(*m&&(*p|32)==*m){p++;m++;}
        if(!*m&&(!*p||*p==';'||*p==' '))return 1;
    }
    return 0;
}
static void wv_run_scripts(void){
    unsigned pages=256,used=0;
    uint8_t *bundle=web_image_pages(pages);job_js_done=job_js_error=job_js_used=0;
    for(script_source *s=job_scripts;bundle&&s&&!image_cancelled();s=s->next){
        char url[WV_URL_MAX];
        if(http_resolve_url(image_base,s->url,url,sizeof(url))||!same_site(image_base,url))continue;
        if(pages*4096-used<=8){job_js_error=2;break;}
        /* Reuse the anonymous resource loader. No Cookie, password, local
         * file, or cross-origin redirect can be supplied to the renderer. */
        if(http_fetch_style(url,image_base,bundle+used+4,pages*4096-used-8,3500,&job_result,image_cancelled)||
           job_result.status!=200||job_result.truncated||!js_mime(job_result.content_type))continue;
        unsigned n=job_result.body_len;
        if(!n)continue;
        memcpy(bundle+used,&n,4);used+=n+4;job_js_done++;
    }
    release_scripts(job_scripts);job_scripts=0;
    if(bundle&&!image_cancelled()&&used>0&&!job_js_error){
        memset(bundle+used,0,4);used+=4;
        unsigned keep=(used+4095)/4096;
        if(keep<pages){web_image_release(bundle+keep*4096,pages-keep);pages=keep;}
        job_js_pages=8;job_js_bytes=web_image_pages(job_js_pages);
        if(job_js_bytes){
            uint32_t utc=0;
            (void)rtc_unix(&utc);
            web_ipc ipc={.source=bundle,.output=(uint8_t*)job_js_bytes,.source_len=used,.cap=job_js_pages*4096,
                 .utc=utc,.deadline=g_pit_ticks+2500,.generation=&generation,.expected=job_generation,.dom_text=live_dom_text};
            int pid, rc=exec_webjs(&ipc,&pid);
            job_js_error=rc?3:(unsigned)task_wait_pid(pid);
            if(!job_js_error)job_js_used=ipc.out_at;
        } else {job_js_pages=0;job_js_error=3;}
    } else if(!bundle)job_js_error=3;
    if(bundle)web_image_release(bundle,pages);
}

static void wv_worker(void *unused)
{
    (void)unused;
    if(script_job){
        wv_run_scripts();
        __atomic_store_n(&job_state,2,__ATOMIC_RELEASE);
        return;
    }
    /* The document is already parsed, but its small body buffer cannot hold
     * ordinary site banners. Borrow pages for this one download and return
     * them before publishing, including on cancellation and decode failure. */
    unsigned cap=WV_PAGE_MAX, pages=384;
    uint8_t *extra=(image_job||style_job)?web_image_pages(pages):0;
    uint8_t *data=extra?extra:wv_page;
    if(extra)cap=pages*4096;
    job_rc = style_job ? http_fetch_style(job_url,image_base,data,cap,3500,&job_result,image_cancelled)
           : image_job ? http_fetch_image(job_url,image_base,data,cap,3500,&job_result,image_cancelled)
                       : http_fetch_web(job_url,wv_page,WV_PAGE_MAX,8000,&job_result,
                                        job_session,job_cookies,job_post,job_post_len,image_cancelled);
    if(!image_job&&!style_job){auth_wipe(job_post,sizeof(job_post));job_post_len=0;}
    if(!style_job && !job_rc && job_result.status==200 && !job_result.truncated &&
       (!image_job || !image_cancelled()) &&
       image_mime(job_result.content_type))web_decode_image(data,job_result.body_len,&job_bitmap);
    if(style_job&&!job_rc&&!image_cancelled()&&job_result.status==200&&!job_result.truncated&&job_result.body_len){
        const char *p=job_result.content_type,*m="text/css";
        while(*m && (*p|32)==*m){p++;m++;}
        if(!*m&&(!*p||*p==';'||*p==' ')){
            unsigned n=(job_result.body_len+4095)/4096;
            if(extra){
                if(n<pages)web_image_release(extra+n*4096,pages-n);
                job_style=extra;job_style_pages=n;extra=0;
            } else {
                job_style=web_image_pages(n);
                if(job_style){job_style_pages=n;memcpy(job_style,data,job_result.body_len);}
            }
        }
    }
    if(extra)web_image_release(extra,pages);
    __atomic_store_n(&job_state, 2, __ATOMIC_RELEASE);
}

void wv_cancel(void)
{
    __atomic_add_fetch(&generation,1,__ATOMIC_RELAXED);
    queued = loading = 0;
    auth_wipe(queued_post,sizeof(queued_post));queued_post_len=0;
    image_scan=0;
    styles_pending=0;
    release_scripts(script_head);script_head=script_tail=0;
}
void wv_forget_session(void){wv_cancel();form_reset();web_session_clear();}
void wv_close(void){wv_forget_session();clear_images();clear_style();clear_js();}
unsigned wv_css_count(void){return css_downloads();}
unsigned wv_css_backgrounds(void){return css_backgrounds();}
unsigned wv_image_count(void){
    unsigned n=0;
    for(unsigned i=0;i<picture_count;i++)if(pictures[i].bmp.rgb)n++;
    return n;
}

/* 内容区几何（浏览器窗口给出；wv_layout 用它算每行能放几个字） */
static int wv_view_w = WV_PAGE_W;
static int wv_view_h = WV_PAGE_H;
static uint32_t layout_fg=CSS_UNSET, layout_bg=CSS_UNSET;
static unsigned layout_size,layout_align;
static int layout_left,layout_width;

#define WV_LH 18      /* 16px CJK glyphs plus space for an underline */
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
    L->y = y;
    L->indent = (uint16_t)(indent+layout_left);
    L->fs = fs;
    L->fx = fx;
    L->scale = scale;
    L->text_off = (uint16_t)off;
    L->text_len = (uint16_t)len;
    L->href = href;
    L->fg=layout_fg;L->bg=layout_bg;L->size=layout_size;
    L->extent=layout_width>indent?(uint16_t)(layout_width-indent):1;
}

/* Use the same 8/16px advances for wrapping, drawing and link hit tests. */
static int wv_layout_run(int y, int indent, uint8_t fs, uint8_t fx,
                         uint8_t scale, const uint8_t *src, uint32_t len,
                         uint16_t href, uint32_t base)
{
    int avail=layout_width-indent-WV_MARGIN;
    if(avail<16) avail=16;
    for(uint32_t start=0;start<len;){
        if(wv_line_n>=WV_LINES_MAX-1){wv_truncated=1;break;}
        uint32_t end=start, space=start;
        int width=0;
        while(end<len){
            if((fs&WV_FS_PRE) && src[end]=='\n') break;
            uint32_t bytes;
            int cell=layout_size?gfx_css_cell((const char *)src+end,len-end,&bytes,layout_size)
                                :gfx_utf8_cell((const char *)src+end,len-end,&bytes);
            if(end>start && width+cell>avail) break;
            if(src[end]==' ') space=end+1;
            width+=cell;
            end+=bytes;
        }
        int newline=end<len && (fs&WV_FS_PRE) && src[end]=='\n';
        if(!newline && end<len && space>start && !(fs&WV_FS_PRE)) end=space;
        uint32_t ink=end;
        if(!(fs&WV_FS_PRE)) while(ink>start && src[ink-1]==' ') ink--;
        if(ink>start){
            int x=indent;
            if(layout_align){
                int ink_width=0;
                for(uint32_t k=start;k<ink;){
                    uint32_t b;
                    ink_width+=layout_size?gfx_css_cell((const char *)src+k,ink-k,&b,layout_size):
                        gfx_utf8_cell((const char *)src+k,ink-k,&b);k+=b;
                }
                if(ink_width<avail)x+=(avail-ink_width)/(layout_align==1?2:1);
            }
            wv_emit(y,x,fs,fx,scale,base+start,ink-start,href);
        }
        if(ink>start || newline) y+=layout_size?layout_size+2:WV_LH;
        start=end+(uint32_t)newline;
        if(!(fs&WV_FS_PRE)) while(start<len && src[start]==' ') start++;
    }
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
static const css_style reader_style={CSS_UNSET,CSS_UNSET,0,0,0,0};
static int picture_width(const web_bitmap *b,int extent){
    int avail=extent-WV_MARGIN;
    if(avail<1)avail=1;
    return b->w>(unsigned)avail?avail:(int)b->w;
}
static int picture_height(const web_bitmap *b,int w){return (int)((b->h*w+b->w-1)/b->w);}

static void wv_layout(void)
{
    wv_line_n = 0;
    wv_truncated = 0;
    wv_next_scale = 1;
    wv_tallest=WV_LH;
    layout_fg=layout_bg=CSS_UNSET;layout_size=layout_align=0;
    layout_left=0;layout_width=wv_view_w;
    if(!reader_mode)css_flow_reset(wv_view_w,wv_view_h);

    /* 页面标题排最上面（Rust 已把它单独抓出来放 str 竞技场） */
    wv_title[0] = 0;
    int y = WV_MARGIN;
    if (wv_title_len > 0 && wv_title_off > 0) {
        uint32_t o = wv_title_off - 1;
        uint32_t n = wv_title_len;
        if (o + n > WV_STR_MAX) n = WV_STR_MAX - o;
        uint32_t count=n<sizeof(wv_title)-1 ? n : sizeof(wv_title)-1;
        for (uint32_t j=0;j<count;j++) wv_title[j]=(char)wv_str[o+j];
        wv_title[count]=0;
        y = wv_layout_run(y, WV_MARGIN, WV_FS_BOLD, WV_FX_TITLE | WV_FX_IN_STR, 1,
                          wv_str + o, n, 0, o);
        y += 4;
        wv_emit(y, WV_MARGIN, 0, WV_FX_MUTED, 0, 0, 0, 0);
        y += 8;
    }

    int indent = 0;
    wv_laid_out=0;
    image_scan=picture_count!=0||backgrounds_allowed;
    for (uint32_t i = 0; i < wv_node_count && wv_line_n < WV_LINES_MAX; i++) {
        const wv_node_t *N = &wv_node[i];
        const css_style *style=reader_mode?&reader_style:css_node(i);
        if(!reader_mode){
            int prev_left=layout_left;
            y=css_flow_step(i,y,&layout_left,&layout_width);
            if(prev_left!=layout_left)indent=0;
        }
        wv_laid_out=i+1;
        wv_anchor_y[i]=style->hidden&&N->kind==WV_RASTER_NODE?-1:y;
        if(style->hidden)continue;
        layout_fg=style->fg;layout_bg=!reader_mode&&css_flow_decorated()?CSS_UNSET:style->bg;layout_align=style->align;
        layout_size=style->size;
        if(text_zoom!=100)layout_size=((layout_size?layout_size:16)*text_zoom+50)/100;
        if(layout_size>(unsigned)wv_view_h)layout_size=(unsigned)wv_view_h;
        if(!reader_mode&&dom_ends[i]>i&&js_bytes){
            unsigned start=dom_offsets[i],n=dom_lengths[i];
            if(n)y=wv_layout_run(y,indent,WV_FS_PRE,WV_FX_JS,1,(const uint8_t*)js_bytes+start,n,0,start);
            i=dom_ends[i]-1;
            continue;
        }
        if(N->kind==WV_RASTER_NODE){
            int slot=picture_node[i]-1;
            if(slot>=0 && pictures[slot].bmp.rgb){
                const web_bitmap *b=&pictures[slot].bmp;
                wv_emit(y,indent,0,0,WV_RASTER_LINE,(unsigned)slot,0,N->href_off);
                y+=picture_height(b,picture_width(b,layout_width-indent))+2;
                if(i+1<wv_node_count && wv_node[i+1].kind==WV_N_IMG && wv_node[i+1].level)i++;
            }
            continue;
        }
        if(N->kind==WV_N_ANCHOR){wv_anchor_y[i]=y; continue;}
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
                              (N->href_off?WV_FX_LINK:WV_FX_MUTED) | WV_FX_IN_STR, 1,
                              wv_str + o, n, N->href_off, o);
            continue;
        }
        if (N->kind == WV_N_BLOCK) {
            uint8_t tag = N->tag, level = N->level;
            int gap = wv_block_gap(tag, level)+(int)(style->gap>(unsigned)wv_view_h?(unsigned)wv_view_h:style->gap);
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
                wv_next_scale=(tag<=WV_T_H2) ? 2 : 1;
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

    if(!reader_mode)y=css_flow_step(wv_node_count,y,&layout_left,&layout_width);
    layout_left=0;layout_width=wv_view_w;
    y += WV_MARGIN;
    if (wv_truncated) {
        /* 明确告诉用户"没显示完"，别让人以为这就是全文 */
        static const char msg[] = "... [page truncated: Cerulia limit]";
        wv_emit(y, WV_MARGIN, 0, WV_FX_MUTED | WV_FX_LITERAL, 1,
                0, (uint32_t)(sizeof(msg) - 1), 0);
        y += WV_LH + 2;
    }
    /* Flex siblings restart at the same Y. Keep paint and hit coordinates in
     * visual order; strict comparison preserves DOM order at equal Y. */
    for(uint32_t i=1;i<wv_line_n;i++)if(wv_line[i].y<wv_line[i-1].y){
        wv_line_t saved=wv_line[i];uint32_t j=i;
        while(j&&wv_line[j-1].y>saved.y){wv_line[j]=wv_line[j-1];j--;}
        wv_line[j]=saved;
    }
    for(uint32_t i=0;i<wv_line_n;i++){
        wv_line_t *l=&wv_line[i];
        int h=l->size?l->size+2:WV_LH;
        if(l->scale==WV_RASTER_LINE && pictures && pictures[l->text_off].bmp.rgb){
            const web_bitmap *b=&pictures[l->text_off].bmp;
            h=picture_height(b,picture_width(b,l->extent));
        }
        if(h>wv_tallest)wv_tallest=h;
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
static char document_url[WV_URL_MAX]; /* final URL without its fragment */
static char pinned_fragment[WV_URL_MAX];
static const char *wv_hash(const char *s) { while(*s && *s!='#') s++; return s; }
static int wv_hex(char c) {
    if(c>='0' && c<='9') return c-'0';
    c|=32; return c>='a' && c<='f' ? c-'a'+10 : -1;
}
static void wv_scroll_clamp(int v)
{
    int page=wv_page_h-(wv_view_h>0?wv_view_h:WV_PAGE_H);
    if(page<0)page=0;
    wv_scroll=v<0?0:v>page?page:v;
    if(picture_count||backgrounds_allowed)image_scan=1;
}
static void wv_jump(const char *hash)
{
    if(*hash!='#' || !hash[1]) {pinned_fragment[0]=0;wv_scroll_clamp(0);return;}
    if(hash!=pinned_fragment)wv_cpy(pinned_fragment,sizeof(pinned_fragment),hash);
    uint8_t id[255]; int n=0;
    for(const char *p=hash+1; *p; p++) {
        if(n==(int)sizeof(id)) return;
        if(*p=='%' && p[1] && p[2] && wv_hex(p[1])>=0 && wv_hex(p[2])>=0){
            id[n++]=(uint8_t)(wv_hex(p[1])*16+wv_hex(p[2])); p+=2;
        } else id[n++]=(uint8_t)*p;
    }
    for(uint32_t i=0;i<wv_laid_out;i++){
        const wv_node_t *node=&wv_node[i];
        if(node->kind!=WV_N_ANCHOR || node->text_len!=n) continue;
        int j=0; while(j<n && wv_str[node->text_off+j]==id[j]) j++;
        if(j==n) {wv_scroll_clamp(wv_anchor_y[i]); return;}
    }
    if(n==3 && (id[0]|32)=='t' && (id[1]|32)=='o' && (id[2]|32)=='p') wv_scroll_clamp(0);
}
static void wv_reflow(void){
    int cut=wv_truncated;wv_layout();wv_truncated|=cut;
    if(pinned_fragment[0])wv_jump(pinned_fragment);
    else wv_scroll_clamp(wv_scroll);
}
static int wv_problem_reader(const char *url){
    const char *prefixes[]={"https://www.luogu.com.cn/problem/", "https://luogu.com.cn/problem/"};
    for(unsigned i=0;i<2;i++){
        const char *p=prefixes[i], *s=url;
        while(*p && *s==*p){s++;p++;}
        if(!*p && *s && *s!='?' && *s!='/') return 1;
    }
    return 0;
}

static int wv_case_equal(const char *start,const char *end,const char *key)
{
    while(start<end && *key){
        char a=*start++,b=*key++;
        if(a>='A'&&a<='Z') a+=32;
        if(a!=b) return 0;
    }
    return start==end && !*key;
}

enum wv_encoding { WV_ENCODING_NONE, WV_ENCODING_UTF8, WV_ENCODING_GBK };

static int wv_html_space(char c)
{
    return c==' ' || c=='\t' || c=='\n' || c=='\r' || c=='\f';
}

static enum wv_encoding wv_label(const char *p,const char *end)
{
    while(p<end && wv_html_space(*p)) p++;
    while(p<end && wv_html_space(end[-1])) end--;
    if(wv_case_equal(p,end,"utf-8") || wv_case_equal(p,end,"utf8")) return WV_ENCODING_UTF8;
    if(wv_case_equal(p,end,"gbk") || wv_case_equal(p,end,"gb2312") ||
       wv_case_equal(p,end,"windows-936") || wv_case_equal(p,end,"x-gbk"))
        return WV_ENCODING_GBK;
    return WV_ENCODING_NONE;
}

static enum wv_encoding wv_charset_param(const char *p,const char *end)
{
    while(p<end){
        while(p<end && (*p==';' || wv_html_space(*p))) p++;
        const char *key=p;
        while(p<end && *p!=';' && *p!='=' && !wv_html_space(*p)) p++;
        int charset=wv_case_equal(key,p,"charset");
        while(p<end && wv_html_space(*p)) p++;
        if(p<end && *p=='='){
            p++;
            while(p<end && wv_html_space(*p)) p++;
            char quote=(p<end && (*p=='"' || *p=='\'')) ? *p++ : 0;
            const char *value=p;
            while(p<end && (quote ? *p!=quote : (*p!=';' && !wv_html_space(*p)))) p++;
            if(quote && p==end) return WV_ENCODING_NONE;
            if(charset) return wv_label(value,p);
            if(quote) p++;
        }
        while(p<end && *p!=';') p++;
    }
    return WV_ENCODING_NONE;
}

/* A bounded prescan, not a second HTML parser. Use real early meta tags,
 * not substrings in attributes, comments or script/style/title text. */
static enum wv_encoding wv_meta_charset(const uint8_t *input,uint32_t size)
{
    const char *p=(const char *)input, *end=p+(size<1024u ? size : 1024u);
    const char *raw=0;
    while(p<end){
        if(*p++!='<') continue;
        if(!raw && end-p>=3 && p[0]=='!' && p[1]=='-' && p[2]=='-'){
            p+=3;
            while(end-p>=3 && !(p[0]=='-' && p[1]=='-' && p[2]=='>')) p++;
            if(end-p<3) break;
            p+=3; continue;
        }
        int closing=p<end && *p=='/';
        if(raw && !closing) continue;
        if(closing) p++;
        const char *tag=p;
        while(p<end && *p!='>' && *p!='/' && *p!='<' && !wv_html_space(*p)) p++;
        if(raw && (!wv_case_equal(tag,p,raw) || p==end || *p=='<')){
            p=tag; /* Keep scanning raw text, including adjacent '<' bytes. */
            continue;
        }
        if(p==tag) continue;
        int meta=!closing && wv_case_equal(tag,p,"meta");
        if(!closing){
            if(wv_case_equal(tag,p,"script")) raw="script";
            else if(wv_case_equal(tag,p,"style")) raw="style";
            else if(wv_case_equal(tag,p,"title")) raw="title";
            else if(wv_case_equal(tag,p,"textarea")) raw="textarea";
        } else if(raw) raw=0;
        const char *label=0,*label_end=0,*pragma=0,*pragma_end=0,*content=0,*content_end=0;
        while(p<end && *p!='>'){
            if(wv_html_space(*p) || *p=='/' || *p=='='){p++;continue;}
            const char *attr=p;
            while(p<end && *p!='>' && *p!='=' && *p!='/' && !wv_html_space(*p)) p++;
            const char *attr_end=p;
            while(p<end && wv_html_space(*p)) p++;
            const char *value=p,*value_end=p;
            if(p<end && *p=='='){
                p++;
                while(p<end && wv_html_space(*p)) p++;
                char quote=(p<end && (*p=='"' || *p=='\'')) ? *p++ : 0;
                value=p;
                while(p<end && (quote ? *p!=quote : (*p!='>' && !wv_html_space(*p)))) p++;
                value_end=p;
                if(quote && p<end) p++;
            }
            if(meta && !label && wv_case_equal(attr,attr_end,"charset")){label=value;label_end=value_end;}
            if(meta && !pragma && wv_case_equal(attr,attr_end,"http-equiv")){pragma=value;pragma_end=value_end;}
            if(meta && !content && wv_case_equal(attr,attr_end,"content")){content=value;content_end=value_end;}
        }
        if(p==end) break;
        p++;
        if(meta){
            enum wv_encoding enc=WV_ENCODING_NONE;
            if(label) enc=wv_label(label,label_end);
            else if(content && pragma && wv_case_equal(pragma,pragma_end,"content-type"))
                enc=wv_charset_param(content,content_end);
            if(enc!=WV_ENCODING_NONE) return enc;
        }
    }
    return WV_ENCODING_NONE;
}

static uint32_t wv_decode_gbk(const uint8_t *src,uint32_t n,int *cut)
{
    extern const uint16_t gbk_unicode[126*190];
    uint32_t i=0,o=0;
    while(i<n){
        uint32_t cp=src[i++];
        if(cp>=0x80){
            uint32_t lead=cp;
            cp=0xfffd;
            if(lead>=0x81 && lead<=0xfe && i<n){
                uint32_t tail=src[i];
                if(tail>=0x40 && tail<=0xfe && tail!=0x7f){
                    uint32_t u=gbk_unicode[(lead-0x81)*190+tail-0x40-(tail>=0x80)];
                    if(u){cp=u;i++;}
                }
            }
        }
        uint32_t bytes=cp<0x80 ? 1 : cp<0x800 ? 2 : 3;
        if(bytes>sizeof(wv_decoded)-o){*cut=1;break;}
        if(bytes==3) wv_decoded[o++]=(uint8_t)(0xe0|(cp>>12));
        if(bytes>=2) wv_decoded[o++]=(uint8_t)((bytes==2?0xc0:0x80)|((cp>>6)&0x3f));
        wv_decoded[o++]=(uint8_t)(bytes==1?cp:0x80|(cp&0x3f));
    }
    return o;
}
static html_form_t form;
static int field_focus, field_width, field_cursor;
static const char *form_note;
#define FORM_H 26
static unsigned fields_height(void){
    return FORM_H*(unsigned)((form.name[0]!=0)+(form.secret_name[0]!=0));
}
static void field_blur(int wipe){
    if(field_focus==2)keyboard_set_private_input(0);
    field_focus=0;
    if(wipe)auth_wipe(form.secret,sizeof(form.secret));
}

static void form_reset(void)
{
    field_blur(1);
    auth_wipe(&form,sizeof(form));
    form_note=0;
}
static void focus_field(int secret)
{
    field_blur(0);
    field_focus=secret?2:1;
    field_cursor=wv_len(secret?form.secret:form.value);
    if(secret)keyboard_set_private_input(1);
    form_note=0;
}
static int wv_prev_byte(const char *text,int i) {
    if(i>0) i--;
    while(i && ((uint8_t)text[i]&0xc0)==0x80) i--;
    return i;
}
static int wv_next_byte(const char *text,int i,int n) {
    uint32_t bytes;
    gfx_utf8_cell(text+i,(uint32_t)(n-i),&bytes);
    return i+(int)bytes;
}
static int same_site(const char *a, const char *b)
{
    /* Include scheme and port. A form may not send typed text off-origin. */
    int i=0;
    for (;a[i] && b[i];i++) {
        if(i>7 && (a[i]=='/' || a[i]=='?' || a[i]=='#')) break;
        char x=a[i],y=b[i];
        if(x>='A'&&x<='Z')x+=32;
        if(y>='A'&&y<='Z')y+=32;
        if(x!=y) return 0;
    }
    return (!a[i] || a[i]=='/' || a[i]=='?' || a[i]=='#')
        && (!b[i] || b[i]=='/' || b[i]=='?' || b[i]=='#');
}
static int https_url(const char *url){
    const char *prefix="https://";
    for(unsigned k=0;prefix[k];k++)if(!url[k]||(url[k]|32)!=prefix[k])return 0;
    return 1;
}
static void form_submit(void)
{
    if (form.state!=2) return;
    if (form.required && !form.value[0]) { form_note="Required"; return; }
    if (form.secret_required && !form.secret[0]) { form_note="Password required"; return; }
    int allow=form_event_rs(&form);
    field_cursor=wv_len(form.value);
    if(allow<=0){form_note=allow ? "Unsupported script" : "Cancelled"; return;}
    char target[WV_URL_MAX];
    if (!form.action[0]) wv_cpy(target,sizeof(target),document_url);
    else if (http_resolve_url(document_url,form.action,target,sizeof(target))) {
        form_note="Invalid action"; return;
    }
    if(!same_site(document_url,target)){form_note="Cross-site blocked"; return;}
    if(form.method){
        if(!https_url(target)){form_note="POST requires HTTPS";return;}
        uint8_t body[sizeof(queued_post)];
        int n=form_query_rs(&form,body,sizeof(body));
        if(n<=0)form_note="Form too large";
        else if(!wv_go(target)){
            memcpy(queued_post,body,(unsigned)n);queued_post_len=(unsigned)n;
        }
        auth_wipe(body,sizeof(body));return;
    }
    int i=0;
    while(target[i] && target[i]!='?' && target[i]!='#') i++;
    if(i+1>=(int)sizeof(target) ||
       form_query_rs(&form,(uint8_t*)target+i+1,sizeof(target)-i-1)<0){
        form_note="Query too long"; return;
    }
    target[i]='?';
    wv_go(target);
}

/* 抓一个 URL 并排版。成功返回 0。 */
static int wv_load(const char *url, int record)
{
    if (!url || !url[0]) return -1;
    form_reset();

    wv_cpy(wv_url, sizeof(wv_url), url);
    clear_images();
    clear_style();
    clear_js();
    pinned_fragment[0]=0;
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

    /* 内置 Dino 页只有 URL 状态，页面由 gfxwin.c 直接绘制。
     * 必须在 http_fetch 之前返回：离线页走网络不仅拖住 GUI，失败后
     * wv_error 还会让本来可用的内置页显示成加载错误。 */
    wv_cancel();
    queued_cookies=!document_url[0]||same_site(document_url,url);
    if (wv_is_dino_url()) {
        if (record) wv_hist_add(wv_url, wv_title);
        return 0;
    }
    if (!netdev_present()) { wv_error = -16; return -16; }
    wv_cpy(queued_url, sizeof(queued_url), url);
    queued = loading = 1;
    record_history = record;
    return 0;
}

static int wv_publish(void)
{
    http_result_t r = job_result;
    int rc = job_rc;
    wv_status = r.status;
    wv_bytes = r.body_len;
    if (rc != 0) { wv_error = rc; return rc; }
    wv_cpy(document_url,sizeof(document_url),r.final_url);
    document_url[wv_hash(document_url)-document_url]=0;
    css_document(document_url);
    if (!wv_bar_focus && wv_streq(wv_url,job_url)) {
        int base=wv_cpy(wv_url,sizeof(wv_url),document_url);
        const char *fragment=wv_hash(job_url);
        if(wv_len(fragment)+base<WV_URL_MAX) wv_cpy(wv_url+base,sizeof(wv_url)-base,fragment);
        wv_bar_cursor=wv_len(wv_url); wv_bar_scroll=0;
    }
    if(image_mime(r.content_type)){
        if(!job_bitmap.rgb){wv_error=-17;return -17;}
        pictures=&single_picture;pictures[0]=(picture){.bmp=job_bitmap,.requested=1};job_bitmap=(web_bitmap){0};
        picture_count=picture_node[0]=1;
        wv_node[0]=(wv_node_t){.kind=WV_RASTER_NODE}; wv_node_count=1;
        wv_layout();return 0;
    }
    if (r.body_len == 0) { wv_page_h = 20; return 0; }

    uint32_t stats[12];
    form.policy=r.form_policy;
    const char *mime="text/html";
    int m=0;
    while(mime[m] && (r.content_type[m]|32)==mime[m]) m++;
    int html_mime=!mime[m] && (!r.content_type[m] || r.content_type[m]==';' || r.content_type[m]==' ');
    const uint8_t *input=wv_page;
    uint32_t length=r.body_len;
    int charset_cut=0;
    int bom=length>=3 && input[0]==0xef && input[1]==0xbb && input[2]==0xbf;
    enum wv_encoding enc=WV_ENCODING_NONE;
    if(bom){
        input+=3; length-=3; enc=WV_ENCODING_UTF8;
    } else if(html_mime) {
        enc=wv_charset_param(r.content_type,r.content_type+wv_len(r.content_type));
        if(enc==WV_ENCODING_NONE) enc=wv_meta_charset(input,length);
    }
    if(enc==WV_ENCODING_GBK){
        length=wv_decode_gbk(input,length,&charset_cut);
        input=wv_decoded;
    }
    style_blocked=(r.form_policy&8)!=0;
    if(html_mime && length && !style_blocked){
        unsigned pages=(length+4095)/4096;
        style_document=web_image_pages(pages);
        if(style_document){
            style_document_pages=pages;memcpy(style_document,input,length);input=style_document;
            styles_pending=1;
        }
    }
    int nodes = html_parse_rs(input, length,
                              (html_node_t *)wv_node, WV_NODES_MAX,
                              wv_text, WV_TEXT_MAX, wv_str, WV_STR_MAX,
                              wv_stack, WV_STACK_MAX, stats,
                              html_mime ? &form : 0, html_mime && r.status==200 && wv_problem_reader(document_url),
                              style_document?css_html:0);
    if (nodes < 0) { wv_error = -5; return -5; }
    if((form.policy&1)||r.truncated||r.status!=200){
        release_scripts(script_head);script_head=script_tail=0;
    }
    if(form.method&&!https_url(document_url))form_reset();

    wv_node_count = stats[0];
    if(html_mime && r.status==200 && !(form.policy&4) && !r.truncated){
        unsigned n=0;
        for(unsigned i=0;i<wv_node_count;i++)n+=wv_node[i].kind==WV_RASTER_NODE;
        if(n){
            unsigned pages=(n*sizeof(picture)+4095)/4096;
            pictures=web_image_pages(pages);
            if(pictures){picture_pages=pages;memset(pictures,0,n*sizeof(picture));}
        }
    }
    if(pictures){
        for(unsigned i=0;i<wv_node_count;i++)if(wv_node[i].kind==WV_RASTER_NODE){
            const char *ref=(const char*)wv_str+wv_node[i].text_off;
            if(!ref[0] || ref[0]=='#')continue;
            unsigned j=0;
            while(j<picture_count && !wv_streq(ref,pictures[j].ref))j++;
            if(j==picture_count){pictures[j].ref=ref;picture_count++;}
            picture_node[i]=j+1;
        }
    }
    if (stats[3]) wv_truncated = 1;
    wv_title_len = stats[5];
    wv_title_off = stats[4];

    backgrounds_allowed=html_mime&&r.status==200&&!(form.policy&4)&&!r.truncated;
    css_resolve(style_blocked,(unsigned)wv_view_w,(unsigned)wv_view_h);
    wv_layout();
    wv_jump(wv_hash(job_url));
    if (r.truncated || stats[3] || charset_cut) wv_truncated = 1;
    return 0;
}

int wv_poll(void)
{
    int changed = 0;
    if (__atomic_load_n(&job_state, __ATOMIC_ACQUIRE) == 2) {
        if (generation == job_generation) {
            if(script_job){
                js_done=job_js_done;js_error=job_js_error;
                if(job_js_used&&!job_js_error){
                    unsigned i=0;
                    while(i+4<=job_js_used){
                        const uint8_t *p=(const uint8_t*)job_js_bytes+i;
                        unsigned id=p[0]|((unsigned)p[1]<<8),n=p[2]|((unsigned)p[3]<<8);
                        i+=4;
                        if(!id||id+n>job_js_used-i)break;
                        unsigned f,t;
                        if(css_dom_range((const uint8_t*)job_js_bytes+i,id,&f,&t)&&f<t&&t<=wv_node_count){
                            dom_offsets[f]=i+id;dom_lengths[f]=n;dom_ends[f]=t;
                        }
                        i+=id+n;
                    }
                    js_bytes=job_js_bytes;js_pages=job_js_pages;
                    job_js_bytes=0;job_js_pages=0;wv_reflow();
                }
                changed=1;
            } else if(style_job){
                if(job_style){
                    css_receive(job_style,job_result.body_len,job_style_pages,job_result.final_url);
                    job_style=0;job_style_pages=0;
                    css_resolve(style_blocked,(unsigned)wv_view_w,(unsigned)wv_view_h);
                    wv_reflow();changed=1;
                }
            } else if(image_job<0){
                if(job_bitmap.rgb)changed=1;
                /* Retry a transient transport error, not a policy denial,
                 * MIME error or a successfully transferred unsupported codec. */
                int retry=job_rc==-2||job_rc==-3||job_rc==-4||job_rc==-5||job_rc==-9||job_rc==-10;
                css_background_received(&job_bitmap,retry);
            } else if(image_job){
                if(job_bitmap.rgb){
                    pictures[image_job-1].bmp=job_bitmap; job_bitmap=(web_bitmap){0};
                    wv_reflow();changed=1;
                }
            } else {
                loading = 0;
                if (wv_publish() == 0 && record_history) wv_hist_add(job_url, wv_title);
                changed = 1;
            }
        }
        web_free_image(&job_bitmap);
        if(job_style){web_image_release(job_style,job_style_pages);job_style=0;job_style_pages=0;}
        if(job_js_pages){web_image_release(job_js_bytes,job_js_pages);job_js_bytes=0;job_js_pages=0;}
        __atomic_store_n(&job_state, 0, __ATOMIC_RELEASE);
    }
    if ((queued || (!reader_mode&&(styles_pending || image_scan || script_head))) && !__atomic_load_n(&job_state, __ATOMIC_ACQUIRE)) {
        image_job=style_job=script_job=0;job_generation=generation;
        if(queued){
            queued=0;wv_cpy(job_url,sizeof(job_url),queued_url);
            job_session=web_session_token();job_cookies=queued_cookies;job_post_len=queued_post_len;
            if(job_post_len)memcpy(job_post,queued_post,job_post_len);
            auth_wipe(queued_post,sizeof(queued_post));queued_post_len=0;
        }
        else if(styles_pending&&css_next(document_url,job_url,sizeof(job_url),style_blocked)){
            wv_cpy(image_base,sizeof(image_base),document_url);style_job=1;
        }
        else if(script_head){
            job_scripts=script_head;script_head=script_tail=0;
            wv_cpy(image_base,sizeof(image_base),document_url);script_job=1;
        }
        else {
            styles_pending=0;image_scan=0;
            if(backgrounds_allowed&&css_next_background(job_url,sizeof(job_url),wv_scroll,wv_view_h)){
                image_job=-1;image_scan=1;wv_cpy(image_base,sizeof(image_base),document_url);
            }
            for(unsigned i=0;!image_job&&i<wv_laid_out;i++){
                unsigned n=picture_node[i];
                if(!n || wv_anchor_y[i]<0 || wv_anchor_y[i]<wv_scroll-wv_view_h ||
                   wv_anchor_y[i]>wv_scroll+2*wv_view_h || pictures[n-1].requested)continue;
                pictures[n-1].requested=1;
                if(http_resolve_url(document_url,pictures[n-1].ref,job_url,sizeof(job_url)))continue;
                wv_cpy(image_base,sizeof(image_base),document_url);image_job=(int)n;image_scan=1;break;
            }
            if(!image_job)return changed;
        }
        uint32_t flags = irq_save_disable();
        __atomic_store_n(&job_state, 1, __ATOMIC_RELEASE);
        int pid = task_create("browser-fetch", wv_worker, 0);
        if (pid < 0) {
            __atomic_store_n(&job_state, 0, __ATOMIC_RELEASE);
            if(script_job){release_scripts(job_scripts);job_scripts=0;js_error=3;changed=1;}
            else if(!image_job&&!style_job){
                auth_wipe(job_post,sizeof(job_post));job_post_len=0;
                wv_error = -9; loading = 0; changed = 1;
            }
        }
        irq_restore(flags);
        if (pid >= 0) task_yield();
    }
    return changed;
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
    wv_close();
    pinned_fragment[0]=0;
    form_reset();

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
                              wv_stack, WV_STACK_MAX, stats, 0, 0, 0);
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
int wv_form_available(void) { return form.state==2; }
int wv_field_active(void) { return field_focus; }
uint32_t wv_heading_links(void) {
    uint32_t count=0;
    for(uint32_t i=0;i<wv_line_n;i++)
        if(wv_line[i].scale==2 && wv_line[i].href && wv_str[wv_line[i].href-1]) count++;
    return count;
}
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
    if(ln->fx & WV_FX_JS)return js_bytes+ln->text_off;
    if (ln->fx & WV_FX_LITERAL) return "... [page truncated: Cerulia limit]";
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
        const char *base = wv_line_text(L);
        for (uint32_t k = 0; k < L->text_len; k++)
            ezos_console_putchar(base[k]);
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
    form_reset();
    wv_close();
    reader_mode=0;
    document_url[0]=0;
    pinned_fragment[0]=0;
    text_zoom=100;
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
    return wv_load(url, 1);
}

int wv_can_back(void)    { return wv_hist_pos > 0; }
int wv_can_forward(void) { return wv_hist_pos >= 0 && wv_hist_pos < wv_hist_n - 1; }

int wv_back(void)
{
    if (!wv_can_back()) return -1;
    wv_hist_pos--;
    return wv_load(wv_hist_url[wv_hist_pos], 0);
}

int wv_forward(void)
{
    if (!wv_can_forward()) return -1;
    wv_hist_pos++;
    return wv_load(wv_hist_url[wv_hist_pos], 0);
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
    const char *link=(const char *)wv_str+(href-1), *hash=wv_hash(link);
    if(*link=='#') wv_cpy(target,sizeof(target),document_url);
    else if(http_resolve_url(document_url,link,target,sizeof(target))) return -1;
    int local=*hash && wv_streq(target,document_url);
    int base=wv_len(target);
    if(base+wv_len(hash)>=WV_URL_MAX) return -1;
    wv_cpy(target+base,sizeof(target)-base,hash);
    if(local){
        wv_jump(hash);
        wv_cpy(wv_url,sizeof(wv_url),target);
        wv_bar_cursor=wv_len(wv_url); wv_bar_scroll=0;
        wv_hist_add(target,wv_title); return 0;
    }
    return wv_go(target);
}

const char *wv_current_url(void)   { return wv_url; }
const char *wv_current_title(void) { return wv_title; }
uint32_t    wv_byte_count(void)    { return wv_bytes; }
int32_t     wv_http_status(void)   { return wv_status; }
unsigned    wv_error_number(void){return wv_error<0?(unsigned)-wv_error:0;}

const char *wv_err_text(int rc)
{
    switch (rc) {
    case -1: return "bad arguments";
    case -2: return "DNS lookup failed";
    case -3: return "connection failed";
    case -4: return "send failed";
    case -5: return "bad response";
    case -6: return "protocol error";
    case -7: return "unsupported URL scheme";
    case -8: return "redirect failed";
    case -9: return "network client busy";
    case -10: return "TLS handshake or transport failed";
    case -11: return "TLS needs a valid UTC clock";
    case -12: return "TLS needs a secure random source";
    case -13: return "TLS certificate verification failed";
    case -14: return "blocked HTTPS downgrade";
    case -15: return "HTTPS requires a DNS name";
    case -16: return "No supported network adapter";
    case -17: return "Image could not be decoded within system limits";
    case -18: return "Cookie header exceeds transport capacity";
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
static void draw_field(int ox,int oy,int secret){
    char masked[sizeof(form.secret)];
    const char *value=secret?masked:form.value;
    int n=wv_len(secret?form.secret:form.value);
    if(secret){for(int i=0;i<n;i++)masked[i]='*';masked[n]=0;}
    int active=field_focus==(secret?2:1),start=0,caret=active?field_cursor:0;
    gfx_fill_rect(ox+4,oy+2,field_width-4,21,WV_C_BAR);
    gfx_draw_rect(ox+4,oy+2,field_width-4,21,active?WV_C_LINK:WV_C_BORDER);
    while(start<caret && gfx_text_width_utf8(value+start,caret-start)>field_width-20)
        start=wv_next_byte(value,start,n);
    gfx_set_clip(ox+6,oy+3,field_width-10,18);
    gfx_draw_text_utf8_row(ox+8,oy+4,value+start,n-start,WV_C_FG,WV_C_BAR);
    if(active)gfx_fill_rect(ox+8+gfx_text_width_utf8(value+start,caret-start),oy+4,1,14,WV_C_LINK);
    else if(!n)gfx_draw_text(ox+8,oy+8,secret?"Password (Tab)":form.method?(form.plain_kind?"Email (Tab)":"Text (Tab)"):"Search (Tab)",WV_C_MUTED,WV_C_BAR);
    gfx_set_clip(-1,-1,0,0);
}

void wv_draw_page(int ox, int oy, int iw, int ih)
{
    if (form.state==2 && ih>(int)fields_height()) {
        field_width=iw-74;
        if(field_width<30) field_width=30;
        int h=(int)fields_height();
        gfx_fill_rect(ox,oy,iw,h,WV_C_BG);
        if(form.name[0])draw_field(ox,oy,0);
        if(form.secret_name[0])draw_field(ox,oy+(form.name[0]?FORM_H:0),1);
        gfx_draw_text(ox+field_width+5,oy+8,form.method?"[ Send ]":"[ Go ]",WV_C_TITLE,WV_C_BG);
        if(form_note) gfx_draw_text(ox+8,oy+7,form_note,WV_C_ERR,WV_C_BAR);
        oy+=h; ih-=h;
    }
    gfx_set_clip(ox, oy, iw, ih);
    uint32_t canvas=reader_mode?CSS_UNSET:css_canvas();
    if(canvas<=0xffffff)gfx_rgb_fill(ox,oy,iw,ih,canvas);
    else gfx_fill_rect(ox, oy, iw, ih, WV_C_BG);

    /* 内容区尺寸变了就重排：排版是按"每行能放几个字"算的，窗口拉伸后
     * 不重排的话右边会空一大块（或者反过来，文字被裁掉）。重排只在尺寸
     * 真的变了时发生，不是每帧。 */
    if (iw > 8 && ih > 8 && (iw != wv_view_w || ih != wv_view_h)) {
        wv_view_w = iw;
        wv_view_h = ih;
        if (wv_node_count > 0) {css_resolve(style_blocked,(unsigned)wv_view_w,(unsigned)wv_view_h);wv_reflow();}
    }

    if (loading) {
        gfx_draw_text(ox+8, oy+8, "Loading... (Esc to stop)", WV_C_MUTED, WV_C_BG);
        gfx_set_clip(-1,-1,0,0);
        return;
    }
    if (wv_error != 0) {
        gfx_draw_text(ox + 8, oy + 8, wv_err_text(wv_error), WV_C_ERR, WV_C_BG);
        const char *h = "F1: edit URL   F5: reload";
        gfx_draw_text(ox + 8, oy + 8 + WV_LH + 2, h, WV_C_MUTED, WV_C_BG);
        gfx_set_clip(-1, -1, 0, 0);
        return;
    }
    if(!reader_mode)css_paint_boxes(ox,oy,wv_scroll);
    if (wv_line_n == 0 && !wv_status) {
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
        if (L->y + wv_tallest <= wv_scroll) lo = mid + 1;
        else hi = mid;
    }

    for (uint32_t i = lo; i < wv_line_n; i++) {
        const wv_line_t *L = wv_line_ptr(i);
        int y = oy + L->y - wv_scroll;
        if (y > oy + ih) break;
        int x = ox + L->indent;
        if(L->scale==WV_RASTER_LINE){
            const web_bitmap *b=&pictures[L->text_off].bmp;
            if(b->rgb){int w=picture_width(b,L->extent);gfx_bitmap565(x,y,w,picture_height(b,w),b->rgb,b->w,b->h);}
            continue;
        }

        /* scale == 0：分隔线 */
        if (L->scale == 0) {
            gfx_fill_rect(x, y, (int)L->extent-WV_MARGIN, 1, WV_C_MUTED);
            continue;
        }
        /* scale == 3：项目符号（3x3 方块） */
        if (L->scale == 3) {
            gfx_fill_rect(x, y + 2, 3, 3, WV_C_FG);
            continue;
        }
        /* 空行（标题的上下留白标记） */
        if (L->text_len == 0 && !(L->fx & WV_FX_LITERAL)) continue;

        const char *src = wv_line_text(L);

        uint8_t fg = wv_fg_for(L->fs, L->fx);
        uint8_t bg = WV_C_BG;

        if (L->fx & WV_FX_TITLE) fg = WV_C_TITLE;

        int lh=L->size?L->size+2:WV_LH,w;
        if(L->size || L->fg!=CSS_UNSET || L->bg!=CSS_UNSET || canvas<=0xffffff){
            unsigned size=L->size?L->size:16;
            int width=0;
            for(unsigned k=0;k<L->text_len;){
                uint32_t b;width+=gfx_css_cell(src+k,L->text_len-k,&b,size);k+=b;
            }
            if(L->bg<=0xffffff)gfx_rgb_fill(x,y,width,lh,L->bg);
            uint32_t rgb=L->fg<=0xffffff?L->fg:(L->fx&WV_FX_LINK)?0x0078d7:(L->fx&WV_FX_TITLE)?0x005a9e:(L->fx&WV_FX_MUTED)?0x999999:0;
            w=gfx_css_text(x,y,src,L->text_len,size,rgb);
        } else if(!reader_mode&&css_backgrounds()){
            uint32_t rgb=L->fx&WV_FX_LINK?0x0078d7:L->fx&WV_FX_TITLE?0x005a9e:L->fx&WV_FX_MUTED?0x999999:0;
            w=gfx_css_text(x,y,src,L->text_len,16,rgb);
        } else w=gfx_draw_text_utf8_row(x,y,src,L->text_len,fg,bg);
        /* 下划线：<u>/<a> 与标题 */
        if ((L->fs & WV_FS_UNDER) || (L->fx & (WV_FX_LINK | WV_FX_TITLE))) {
            int uw = w - x;
            if(L->fg<=0xffffff)gfx_rgb_fill(x,y+lh-2,uw,1,L->fg);
            else gfx_fill_rect(x, y + lh - 2, uw, 1, fg);
        }
        if (L->fs & WV_FS_STRIKE) {
            int uw = w - x;
            if (x + uw <= ox + iw) gfx_fill_rect(x, y + 8, uw, 1, fg);
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

    /* 输出内容区相对坐标（与 click 回调的 lx/ly 同域），
     * browser_click 做命中测试时才能直接比较 */
    if (bar_x) *bar_x = ax - ox;
    if (bar_y) *bar_y = ay - oy;
    if (bar_w) *bar_w = aw;
    if (bar_h) *bar_h = ah;

    int total = wv_len(wv_url);
    int avail=aw-7, from=0, caret=0;
    if(wv_bar_focus){
        from=wv_bar_scroll>wv_bar_cursor ? wv_bar_cursor : wv_bar_scroll;
        caret=gfx_text_width_utf8(wv_url+from,wv_bar_cursor-from);
        while(from<wv_bar_cursor && caret>avail) {
            uint32_t bytes;
            caret-=gfx_utf8_cell(wv_url+from,wv_bar_cursor-from,&bytes);
            from+=(int)bytes;
        }
    }
    wv_bar_scroll=from;
    int to=from, width=0;
    while(to<total){
        uint32_t bytes;
        int cell=gfx_utf8_cell(wv_url+to,total-to,&bytes);
        if(width+cell>avail) break;
        width+=cell; to+=(int)bytes;
    }
    gfx_set_clip(ax+2,ay+1,aw-4,ah-2);
    gfx_draw_text_utf8_row(ax+3,ay+(ah-16)/2,wv_url+from,to-from,
                          wv_bar_focus&&wv_bar_selected ? WV_C_BAR : WV_C_FG,bar_bg);
    gfx_set_clip(-1,-1,0,0);
    /* 光标：闪烁方块（8x8 字模里没有 _ ） */
    int cx = ax + 3 + caret;
    if (cx < ax + 2) cx = ax + 2;
    if (cx > ax + aw - 3) cx = ax + aw - 3;
    if (wv_bar_focus && ((g_pit_ticks / 30) & 1) == 0)
        gfx_fill_rect(cx, ay + (ah - 8) / 2, 1, 8, WV_C_FG);

    /* 状态 */
    const char *st;
    char code[4];
    if (wv_error) st = "ERR";
    else if (wv_status >= 400 && wv_status <= 999) {
        code[0]=(char)('0'+wv_status/100);
        code[1]=(char)('0'+wv_status/10%10);
        code[2]=(char)('0'+wv_status%10); code[3]=0;
        st=code;
    }
    else if (wv_truncated) st = "CUT";
    else if (!wv_line_n) st = "";
    else st = "OK";
    gfx_draw_text(ox + iw - 22, my, st, WV_C_MUTED, WV_C_TOOLBAR);
}

/* ---------------- 交互 ---------------- */

int wv_bar_has_focus(void) { return wv_bar_focus; }

void wv_bar_set_focus(int f)
{
    if (f) field_blur(1);
    wv_bar_focus = f;
    wv_bar_selected=f;
    if (f) { wv_bar_cursor = wv_len(wv_url); wv_bar_scroll = 0; }
}

void wv_bar_key(int key)
{
    if (!wv_bar_focus) return;
    if (wv_bar_selected && ((key>=32 && key<127) || key==8 || key==127)) {
        wv_bar_cursor=0; wv_url[0]=0;
    }
    wv_bar_selected=0;
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
            int prev=wv_prev_byte(wv_url,wv_bar_cursor);
            for (int i=wv_bar_cursor;i<=n;i++) wv_url[prev+i-wv_bar_cursor]=wv_url[i];
            wv_bar_cursor=prev; wv_bar_scroll=0;
        }
        return;
    }
    if (key == KEY_LEFT)  { wv_bar_cursor=wv_prev_byte(wv_url,wv_bar_cursor); return; }
    if (key == KEY_RIGHT) { wv_bar_cursor=wv_next_byte(wv_url,wv_bar_cursor,n); return; }
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
                full[4] = 's'; full[5] = ':'; full[6] = '/'; full[7] = '/';
                wv_cpy(full + 8, sizeof(full) - 8, u);
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
    if(form.state==2 && (key=='\t' || key==KEY_F4)){
        focus_field(!form.name[0] || (field_focus==1&&form.secret_name[0])); return;
    }
    if(form.state==2 && field_focus) {
        char *value=field_focus==2?form.secret:form.value;
        unsigned cap=field_focus==2?sizeof(form.secret):sizeof(form.value);
        int n=wv_len(value);
        form_note=0;
        if(key=='\n'||key=='\r'){
            if(field_focus==1&&form.secret_name[0])focus_field(1);
            else form_submit();
            return;
        }
        if(key==27){field_blur(1);return;}
        if(key==KEY_LEFT){field_cursor=wv_prev_byte(value,field_cursor);return;}
        if(key==KEY_RIGHT){field_cursor=wv_next_byte(value,field_cursor,n);return;}
        if(key==KEY_HOME){field_cursor=0;return;}
        if(key==KEY_END){field_cursor=n;return;}
        if(key==8 || key==127){
            int prev=wv_prev_byte(value,field_cursor);
            for(int i=field_cursor;i<=n;i++) value[prev+i-field_cursor]=value[i];
            field_cursor=prev;
            return;
        }
        if(key>=32 && key<127){
            if(n+1<(int)cap){
                for(int i=n;i>=field_cursor;i--) value[i+1]=value[i];
                value[field_cursor++]=(char)key;
            }
            else form_note="Field full";
            return;
        }
    }
    switch (key) {
    case 27: wv_cancel(); break;
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
    case KEY_F8:   {
        reader_mode=!reader_mode;
        wv_reflow();
        break;
    }
    case KEY_F6: case KEY_F7: {
        /* Clamp on screen geometry, not on website or document size. */
        if(key==KEY_F7 && text_zoom<(unsigned)wv_view_h*100/16)text_zoom+=25;
        if(key==KEY_F6 && text_zoom>25)text_zoom-=25;
        wv_reflow();
        break;
    }
    case '\n': case '\r': wv_bar_set_focus(1); break;
    default: break;
    }
}

void wv_scroll_by(int dy)
{
    if(dy)pinned_fragment[0]=0;
    wv_scroll_clamp(wv_scroll+dy);
}

void wv_scroll_to_top(void) { pinned_fragment[0]=0;wv_scroll_clamp(0); }
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
        if(L->scale==WV_RASTER_LINE){
            const web_bitmap *b=&pictures[L->text_off].bmp;
            if(b->rgb){int w=picture_width(b,L->extent);
                if(py>=L->y && py<L->y+picture_height(b,w) && px>=L->indent && px<L->indent+w)return L->href;}
            continue;
        }
        if (L->text_len == 0) continue;
        if (py < L->y || py >= L->y + (L->size?L->size+2:WV_LH)) continue;
        const char *text=wv_line_text(L);
        int w=0;
        for(unsigned k=0;k<L->text_len;){
            uint32_t b;
            w+=L->size?gfx_css_cell(text+k,L->text_len-k,&b,L->size):gfx_utf8_cell(text+k,L->text_len-k,&b);
            k+=b;
        }
        if (px >= L->indent && px < L->indent + w) return L->href;
    }
    return 0;
}

void wv_page_click(int x,int y)
{
    wv_bar_set_focus(0);
    if(form.state==2){
        int h=(int)fields_height();
        if(y<h){
            if(x>=4 && x<field_width) focus_field(!form.name[0] || (y>=FORM_H&&form.secret_name[0]));
            else if(x>=field_width && x<field_width+65) form_submit();
            return;
        }
        y-=h;
    }
    field_blur(1);
    uint16_t href=wv_hit_link(x,y+wv_scroll);
    if(href) wv_follow(href);
}
