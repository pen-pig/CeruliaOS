#ifndef CERULIA_WEBSESSION_H
#define CERULIA_WEBSESSION_H
#include "types.h"

/* A session lives only until the browser closes, locks or changes users. */
uint32_t web_session_token(void);
void web_session_clear(void);
void web_session_accept(uint32_t token,const char *host,uint16_t port,const char *path,
                        const char *set_cookie,uint32_t now);
/* Serialize just the field value; -1 means the whole Cookie field did not fit. */
int web_session_header(uint32_t token,const char *host,uint16_t port,const char *path,
                      char *out,unsigned cap,uint32_t now);
#endif
