#ifndef AUTH_H
#define AUTH_H
#include "types.h"

#define AUTH_SALT 16
#define AUTH_DIGEST 32
#define AUTH_MIN_PASSWORD 15
#define AUTH_MAX_PASSWORD 64

void auth_hash(const void *data, uint32_t len, uint8_t out[AUTH_DIGEST]);
void auth_salt(uint8_t out[AUTH_SALT], uint32_t sequence);
void auth_password(const char *password, uint32_t len,
                   const uint8_t salt[AUTH_SALT], uint8_t out[AUTH_DIGEST]);
int auth_equal(const void *a, const void *b, uint32_t len);
void auth_wipe(void *data, uint32_t len);
#endif
