#ifndef CERULIA_WEBIMAGE_H
#define CERULIA_WEBIMAGE_H
/* Owned compact RGB565 thumbnail, allocated in contiguous PMM pages. */
typedef struct { unsigned short *rgb; unsigned w,h,pages; } web_bitmap;
int web_decode_image(unsigned char *src, unsigned len, web_bitmap *out);
void web_free_image(web_bitmap *b);
void *web_image_pages(unsigned n);
void web_image_release(void *p,unsigned n);
#endif
