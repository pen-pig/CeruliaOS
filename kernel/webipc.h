#ifndef CERULIA_WEBIPC_H
#define CERULIA_WEBIPC_H
#include "types.h"
/* One owner and one sandbox task. Buffers remain valid until the owner
 * reaps the renderer. No capabilities are inherited from the desktop. */
typedef struct web_ipc {
    const uint8_t *source;
    uint8_t *output;
    uint32_t source_len,cap,in_at,out_at,utc,deadline;
    const uint32_t *generation;
    uint32_t expected;
    /* Return UTF-8 length, or -1 for an unavailable element. A NULL
     * destination measures without writing; the syscall validates the copy. */
    int (*dom_text)(const uint8_t *,uint32_t,uint8_t *);
} web_ipc;
#endif
