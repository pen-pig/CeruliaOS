#pragma once
#include <stddef.h>
/* All decoder instances are caller-allocated. Disable the convenience API. */
static inline void *calloc(size_t n,size_t s){(void)n;(void)s;return NULL;}
static inline void free(void *p){(void)p;}
