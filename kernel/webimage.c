/* Untrusted raster bytes -> bounded thumbnail. One asynchronous decoder,
 * no calls into the desktop, no VFS or OS-side browser dependencies. */
#define WUFFS_IMPLEMENTATION
#define WUFFS_CONFIG__MODULES
#define WUFFS_CONFIG__MODULE__BASE
#define WUFFS_CONFIG__MODULE__ADLER32
#define WUFFS_CONFIG__MODULE__CRC32
#define WUFFS_CONFIG__MODULE__DEFLATE
#define WUFFS_CONFIG__MODULE__ZLIB
#define WUFFS_CONFIG__MODULE__JPEG
#define WUFFS_CONFIG__MODULE__PNG
#define WUFFS_CONFIG__STATIC_FUNCTIONS
#define WUFFS_CONFIG__AVOID_CPU_ARCH
#define WUFFS_CONFIG__DST_PIXEL_FORMAT__ENABLE_ALLOWLIST
#define WUFFS_CONFIG__DST_PIXEL_FORMAT__ALLOW_BGR_565
#include "../third_party/wuffs/wuffs-v0.4.c"
#include "../third_party/tjpgdec/tjpgd.c"
#include "webimage.h"

static unsigned pages(unsigned n){return (n+4095)/4096;}
static void *reserve(unsigned n){return web_image_pages(pages(n));}
static void release(void *p,unsigned n){if(p)web_image_release(p,pages(n));}
void web_free_image(web_bitmap *b){
    if(b->rgb)web_image_release(b->rgb,b->pages);
    *b=(web_bitmap){0};
}
typedef struct {
    const uint8_t *src; size_t left;
    uint16_t *dst; unsigned w,h,tw,th,written;
} thumb_job;
static size_t jpeg_in(JDEC *dec,uint8_t *dst,size_t n){
    thumb_job *j=dec->device;
    if(n>j->left)n=j->left;
    if(dst && n)memcpy(dst,j->src,n);
    if(n)j->src+=n;
    j->left-=n;return n;
}
static int jpeg_out(JDEC *dec,void *raw,JRECT *r){
    thumb_job *j=dec->device;
    if(r->right>=j->w||r->bottom>=j->h||r->left>r->right||r->top>r->bottom)return 0;
    unsigned l=(r->left*j->tw+j->w-1)/j->w, t=(r->top*j->th+j->h-1)/j->h;
    unsigned rr=((r->right+1)*j->tw+j->w-1)/j->w, b=((r->bottom+1)*j->th+j->h-1)/j->h;
    const uint16_t *src=raw;
    unsigned stride=r->right+1-r->left;
    for(unsigned y=t;y<b;y++)for(unsigned x=l;x<rr;x++)
        j->dst[y*j->tw+x]=src[(y*j->h/j->th-r->top)*stride+x*j->w/j->tw-r->left];
    j->written+=(rr-l)*(b-t);return 1;
}
static int jpeg_thumbnail(uint8_t *src,unsigned len,web_bitmap *out){
    const unsigned need=16384;
    void *work=reserve(need);if(!work)return 0;
    thumb_job j={.src=src,.left=len};
    JDEC dec={0};
    int ok=0;
    if(jd_prepare(&dec,jpeg_in,work,need,&j)==JDR_OK && dec.width && dec.height){
        unsigned w=dec.width,h=dec.height;
        unsigned scale=0;
        while(scale<3 && ((w>>(scale+1))>=640 || (h>>(scale+1))>=400) &&
              (w>>(scale+1)) && (h>>(scale+1)))scale++;
        j.w=w>>scale;j.h=h>>scale;j.tw=j.w;j.th=j.h;
        if(j.tw>640){j.tw=640;j.th=(j.h*j.tw+j.w-1)/j.w;}
        if(j.th>400){j.th=400;j.tw=(j.w*j.th+j.h-1)/j.h;}
        unsigned size=j.tw*j.th*2;
        j.dst=reserve(size);
        if(j.dst && jd_decomp(&dec,jpeg_out,scale)==JDR_OK && j.written==j.tw*j.th){
            *out=(web_bitmap){j.dst,j.tw,j.th,pages(size)};ok=1;
        } else release(j.dst,size);
    }
    release(work,need);return ok;
}
int web_decode_image(uint8_t *src,unsigned len,web_bitmap *out){
    int png=len>=8 && !memcmp(src,"\211PNG\r\n\032\n",8);
    int jpg=len>=3 && src[0]==255 && src[1]==216 && src[2]==255;
    if(!png && !jpg)return 0;
    unsigned ds=png?sizeof(wuffs_png__decoder):sizeof(wuffs_jpeg__decoder);
    void *state=reserve(ds),*work=0; uint16_t *pix=0; unsigned ws=0,ps=0;
    if(!state)return 0;
    wuffs_base__status st=png?wuffs_png__decoder__initialize(state,ds,WUFFS_VERSION,0):
                              wuffs_jpeg__decoder__initialize(state,ds,WUFFS_VERSION,0);
    wuffs_base__image_decoder *dec=png?wuffs_png__decoder__upcast_as__wuffs_base__image_decoder(state):
                                       wuffs_jpeg__decoder__upcast_as__wuffs_base__image_decoder(state);
    wuffs_base__io_buffer in=wuffs_base__ptr_u8__reader(src,len,true);
    wuffs_base__image_config cfg={0};
    int ok=0,large_jpeg=0;
    if(st.repr || wuffs_base__image_decoder__decode_image_config(dec,&cfg,&in).repr)goto done;
    unsigned w=wuffs_base__pixel_config__width(&cfg.pixcfg),h=wuffs_base__pixel_config__height(&cfg.pixcfg);
    if(!w||!h)goto done;
    if(w>4096||h>4096||w*h>1280*1024){large_jpeg=jpg;goto done;}
    uint64_t need=wuffs_base__image_decoder__workbuf_len(dec).max_incl;
    if(need>768*1024){large_jpeg=jpg;goto done;}
    ws=(unsigned)need;
    if(ws && !(work=reserve(ws)))goto done;
    ps=w*h*2;
    if(!(pix=reserve(ps)))goto done;
    for(unsigned i=0;i<w*h;i++)pix[i]=(30u<<11)|(60u<<5)|30u;
    wuffs_base__pixel_buffer pb={0};
    wuffs_base__pixel_config__set(&cfg.pixcfg,WUFFS_BASE__PIXEL_FORMAT__BGR_565,0,w,h);
    if(wuffs_base__pixel_buffer__set_from_slice(&pb,&cfg.pixcfg,wuffs_base__make_slice_u8((uint8_t*)pix,ps)).repr ||
       wuffs_base__image_decoder__decode_frame(dec,&pb,&in,WUFFS_BASE__PIXEL_BLEND__SRC_OVER,
                         wuffs_base__make_slice_u8(work,ws),NULL).repr)goto done;
    unsigned tw=w,th=h;
    if(tw>320){tw=320;th=(h*tw+w-1)/w;}
    if(th>200){th=200;tw=(w*th+h-1)/h;}
    uint16_t *rgb=reserve(tw*th*2);
    if(!rgb)goto done;
    for(unsigned y=0;y<th;y++)for(unsigned x=0;x<tw;x++){
        rgb[y*tw+x]=pix[(y*h/th)*w+x*w/tw];
    }
    *out=(web_bitmap){rgb,tw,th,pages(tw*th*2)}; ok=1;
done:
    release(pix,ps);release(work,ws);release(state,ds);
    return large_jpeg?jpeg_thumbnail(src,len,out):ok;
}
