#ifndef CERULIA_WEBCSS_H
#define CERULIA_WEBCSS_H
#include "types.h"
#include "webimage.h"

#define CSS_UNSET 0xffffffffu
typedef struct {
    uint32_t fg,bg;
    unsigned hidden, align, gap, size;
} css_style;
/* Text/sheet slices point into the owned UTF-8 document, never the HTTP buffer. */
void css_clear(void);
void css_html(unsigned event,const uint8_t *a,unsigned an,const uint8_t *b,unsigned bn);
int css_next(const char *base,char *url,unsigned cap,unsigned blocked);
void css_receive(uint8_t *body,unsigned length,unsigned pages,const char *final_url);
void css_document(const char *final_url);
void css_resolve(unsigned blocked,unsigned width,unsigned height);
const css_style *css_node(unsigned node);
unsigned css_downloads(void);
uint32_t css_canvas(void);
void css_flow_reset(int width,int height);
/* Close previous boxes and open the current element's laid-out ancestors. */
int css_flow_step(unsigned node,int y,int *left,int *width);
int css_flow_decorated(void);
void css_paint_boxes(int x,int y,int scroll);
int css_next_background(char *url,unsigned cap,int top,int height);
void css_background_received(web_bitmap *bmp,int retry);
unsigned css_backgrounds(void);
int css_dom_range(const uint8_t *id,unsigned n,unsigned *first,unsigned *last);
#endif
