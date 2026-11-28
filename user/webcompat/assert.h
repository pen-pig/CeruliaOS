#pragma once
#include <stdlib.h>
/* Keep engine assertions. A failed invariant terminates only the renderer. */
#define assert(x) ((x)?(void)0:abort())
