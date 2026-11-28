#pragma once
#include <stddef.h>
void *malloc(size_t);
void *calloc(size_t,size_t);
void *realloc(void *,size_t);
void free(void *);
_Noreturn void abort(void);
int abs(int);
long labs(long);
#define alloca(n) __builtin_alloca(n)
