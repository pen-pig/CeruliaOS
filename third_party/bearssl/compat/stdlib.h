/* GCC's x86 intrinsic headers declare optional allocation helpers even when
 * all BearSSL hardware backends are disabled. None are linked or called. */
#include <stddef.h>
void *malloc(size_t);
void free(void *);
