/* Host facilities used by the OTCC-derived compiler, implemented using libc. */
#include "libc.h"
#include "cc_runtime_data.h"
#define ALLOC_SIZE 16384
static char source[ALLOC_SIZE + 1];
static unsigned int source_pos;
static void error(const char *message) __attribute__((noreturn));
static int isalnum(int c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
           (c >= '0' && c <= '9') || c == '_';
}
static int isdigit(int c) { return c >= '0' && c <= '9'; }
static int isspace(int c) { return c == ' ' || (c >= 9 && c <= 13); }
static char *strstr(const char *a, const char *b) {
    unsigned int n = strlen(b);
    for (; *a; a++) if (!strncmp(a, b, n)) return (char *)a;
    return n ? 0 : (char *)a;
}
static int parse_number(const char *s) {
    unsigned int v = 0;
    unsigned int base = *s == '0' ? 8u : 10u;
    if (base == 8 && (s[1] == 'x' || s[1] == 'X')) {
        s += 2;
        if (!isalnum(*s)) error("invalid hexadecimal literal");
        base = 16;
    }
    while (isalnum(*s)) {
        int d = *s >= '0' && *s <= '9' ? *s - '0' : (*s | 32) - 'a' + 10;
        if (d < 0 || (unsigned int)d >= base || *s == '_')
            error("invalid integer literal");
        if (v > (0xffffffffu - (unsigned int)d) / base)
            error("integer literal too large");
        v = v * base + (unsigned int)d;
        s++;
    }
    return (int)v;
}
static void *calloc(int count, int size) {
    unsigned int n = (unsigned int)count * (unsigned int)size;
    void *p = malloc(n);
    if (!p) error("compiler workspace exhausted");
    memset(p, 0, n);
    return p;
}

static void error(const char *message) {
    printf("cc: error near byte %u: %s\n", source_pos, message);
    exit(1);
}
