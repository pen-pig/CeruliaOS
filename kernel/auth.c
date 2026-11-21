/* SHA-256 (FIPS 180-4) and PBKDF2-HMAC-SHA256 (RFC 8018), one 32-byte block. */
#include "auth.h"
#include "isr.h"
#include "rtc.h"

#define PBKDF2_ROUNDS 600000u
static const uint32_t initial[8] = {
    0x6a09e667u,0xbb67ae85u,0x3c6ef372u,0xa54ff53au,
    0x510e527fu,0x9b05688cu,0x1f83d9abu,0x5be0cd19u
};
static const uint32_t round_k[64] = {
    0x428a2f98u,0x71374491u,0xb5c0fbcfu,0xe9b5dba5u,0x3956c25bu,0x59f111f1u,0x923f82a4u,0xab1c5ed5u,
    0xd807aa98u,0x12835b01u,0x243185beu,0x550c7dc3u,0x72be5d74u,0x80deb1feu,0x9bdc06a7u,0xc19bf174u,
    0xe49b69c1u,0xefbe4786u,0x0fc19dc6u,0x240ca1ccu,0x2de92c6fu,0x4a7484aau,0x5cb0a9dcu,0x76f988dau,
    0x983e5152u,0xa831c66du,0xb00327c8u,0xbf597fc7u,0xc6e00bf3u,0xd5a79147u,0x06ca6351u,0x14292967u,
    0x27b70a85u,0x2e1b2138u,0x4d2c6dfcu,0x53380d13u,0x650a7354u,0x766a0abbu,0x81c2c92eu,0x92722c85u,
    0xa2bfe8a1u,0xa81a664bu,0xc24b8b70u,0xc76c51a3u,0xd192e819u,0xd6990624u,0xf40e3585u,0x106aa070u,
    0x19a4c116u,0x1e376c08u,0x2748774cu,0x34b0bcb5u,0x391c0cb3u,0x4ed8aa4au,0x5b9cca4fu,0x682e6ff3u,
    0x748f82eeu,0x78a5636fu,0x84c87814u,0x8cc70208u,0x90befffau,0xa4506cebu,0xbef9a3f7u,0xc67178f2u
};

static inline uint32_t ror(uint32_t x, unsigned n) { return (x >> n) | (x << (32u - n)); }
static uint32_t be32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}
static void put32(uint8_t *p, uint32_t x) {
    p[0] = x >> 24; p[1] = x >> 16; p[2] = x >> 8; p[3] = x;
}
static void init(uint32_t s[8], const uint32_t source[8]) {
    for (int i = 0; i < 8; i++) s[i] = source[i];
}
static void compress(uint32_t s[8], const uint8_t block[64]) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++) w[i] = be32(block + i * 4);
    for (int i = 16; i < 64; i++) {
        uint32_t x = w[i-15], y = w[i-2];
        w[i] = w[i-16] + (ror(x,7) ^ ror(x,18) ^ (x>>3)) + w[i-7] +
               (ror(y,17) ^ ror(y,19) ^ (y>>10));
    }
    uint32_t a=s[0],b=s[1],c=s[2],d=s[3],e=s[4],f=s[5],g=s[6],h=s[7];
    for (int i = 0; i < 64; i++) {
        uint32_t t1 = h + (ror(e,6)^ror(e,11)^ror(e,25)) + ((e&f)^(~e&g)) + round_k[i] + w[i];
        uint32_t t2 = (ror(a,2)^ror(a,13)^ror(a,22)) + ((a&b)^(a&c)^(b&c));
        h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
    }
    s[0]+=a; s[1]+=b; s[2]+=c; s[3]+=d; s[4]+=e; s[5]+=f; s[6]+=g; s[7]+=h;
}
static void finish(uint32_t s[8], const uint8_t *data, uint32_t len, uint32_t prefix,
                   uint8_t out[32]) {
    uint8_t block[64];
    uint32_t bytes = prefix + len;
    while (len >= 64) { compress(s, data); data += 64; len -= 64; }
    for (uint32_t i = 0; i < len; i++) block[i] = data[i];
    block[len++] = 0x80;
    for (uint32_t i = len; i < 64; i++) block[i] = 0;
    if (len > 56) {
        compress(s, block);
        for (int i = 0; i < 56; i++) block[i] = 0;
    }
    put32(block + 56, bytes >> 29);
    put32(block + 60, bytes << 3);
    compress(s, block);
    for (int i = 0; i < 8; i++) put32(out + i * 4, s[i]);
}

void auth_hash(const void *data, uint32_t len, uint8_t out[32]) {
    uint32_t s[8];
    init(s, initial);
    finish(s, data, len, 0, out);
}

static void hmac(const uint32_t inner[8], const uint32_t outer[8],
                 const uint8_t *msg, uint32_t len, uint8_t u[32]) {
    uint32_t s[8];
    init(s, inner);
    finish(s, msg, len, 64, u);
    init(s, outer);
    finish(s, u, 32, 64, u);
    auth_wipe(s, sizeof(s));
}

void auth_password(const char *password, uint32_t len, const uint8_t salt[16], uint8_t out[32]) {
    uint8_t block[64], first[20], u[32];
    uint32_t inner[8], outer[8];
    init(inner, initial);
    init(outer, initial);
    for (uint32_t i = 0; i < 64; i++) block[i] = (i < len ? (uint8_t)password[i] : 0) ^ 0x36;
    compress(inner, block);
    for (int i = 0; i < 64; i++) block[i] ^= 0x36 ^ 0x5c;
    compress(outer, block);
    for (int i = 0; i < 16; i++) first[i] = salt[i];
    put32(first + 16, 1);
    hmac(inner, outer, first, sizeof(first), u);
    for (int i = 0; i < 32; i++) out[i] = u[i];
    for (uint32_t n = 1; n < PBKDF2_ROUNDS; n++) {
        hmac(inner, outer, u, sizeof(u), u);
        for (int i = 0; i < 32; i++) out[i] ^= u[i];
    }
    auth_wipe(block, sizeof(block)); auth_wipe(inner, sizeof(inner));
    auth_wipe(outer, sizeof(outer)); auth_wipe(u, sizeof(u));
}

void auth_salt(uint8_t out[16], uint32_t sequence) {
    /* Salt is public; retain uniqueness on older CPUs without RDRAND.
     * Sequence is advanced on each committed database update. */
    uint32_t seed[9] = {0, 0, g_pit_ticks, sequence, 0, 0, 0, 0, 0};
    rtc_unix(&seed[8]);
    uint32_t a = 1, b, c, d;
    asm volatile("cpuid" : "+a"(a), "=b"(b), "=c"(c), "=d"(d));
    if (d & (1u << 4)) asm volatile("rdtsc" : "=a"(seed[0]), "=d"(seed[1]));
    if (c & (1u << 30)) {
        for (int i = 4; i < 8; i++) {
            unsigned char ok;
            uint32_t v;
            asm volatile("rdrand %0; setc %1" : "=r"(v), "=qm"(ok) :: "cc");
            if (ok) seed[i] = v;
        }
    }
    uint8_t digest[32];
    auth_hash(seed, sizeof(seed), digest);
    for (int i = 0; i < 16; i++) out[i] = digest[i];
}

int auth_equal(const void *a, const void *b, uint32_t len) {
    const uint8_t *x = a, *y = b;
    uint8_t different = 0;
    for (uint32_t i = 0; i < len; i++) different |= x[i] ^ y[i];
    return different == 0;
}

void auth_wipe(void *data, uint32_t len) {
    volatile uint8_t *p = data;
    while (len--) *p++ = 0;
}
