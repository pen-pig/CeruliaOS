#pragma once
#include <stddef.h>
void *memcpy(void *, const void *, size_t);
void *memset(void *, int, size_t);
int memcmp(const void *, const void *, size_t);
int strcmp(const char *, const char *);
static inline void *wuffs_move(void *dst, const void *src, size_t n) {
    unsigned char *d=dst;
    const unsigned char *s=src;
    if ((uintptr_t)d < (uintptr_t)s) { for(size_t i=0;i<n;i++) d[i]=s[i]; }
    else if(d!=s) { while(n){ --n; d[n]=s[n]; } }
    return dst;
}
#define memmove wuffs_move
