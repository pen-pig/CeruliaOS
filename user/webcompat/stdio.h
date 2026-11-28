#pragma once
#include <stddef.h>
#include <stdarg.h>
typedef struct FILE {int unused;} FILE;
#define stdout ((FILE*)0)
#define stderr ((FILE*)0)
int vsnprintf(char *,size_t,const char *,va_list);
int snprintf(char *,size_t,const char *,...);
/* Never route engine diagnostic streams into shell or browser data pipes. */
static inline int fprintf(FILE *f,const char *s,...){(void)f;(void)s;return 0;}
static inline int fputs(const char *s,FILE *f){(void)s;(void)f;return 0;}
static inline int fputc(int c,FILE *f){(void)f;return c;}
static inline int putchar(int c){return c;}
static inline size_t fwrite(const void *p,size_t n,size_t c,FILE *f){(void)p;(void)n;(void)f;return c;}
#define printf(...) ((void)0)
#define puts(...) ((void)0)
