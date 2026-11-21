/* Minimal stateless runtime for native i386 programs emitted by cc. */
#include "libc.h"

static int trap(int num, int a, int b, int c) {
    int r;
    __asm__ volatile("int $0x80" : "=a"(r)
                     : "a"(num), "b"(a), "c"(b), "d"(c) : "memory");
    return r;
}
int write(int fd, const void *p, unsigned int n) { return trap(1, fd, (int)p, n); }
int read(int fd, void *p, unsigned int n) { return trap(0, fd, (int)p, n); }
int open(const char *p, int flags) { return trap(5, (int)p, flags, 0); }
int close(int fd) { return trap(6, fd, 0, 0); }
void exit(int code) { trap(2, code, 0, 0); for (;;) {} }

int putchar(int c) {
    unsigned char ch = (unsigned char)c;
    return write(1, &ch, 1) == 1 ? ch : -1;
}

void puts(const char *s) {
    unsigned int n = 0;
    while (s[n]) n++;
    write(1, s, n);
    putchar('\n');
}

static void number(unsigned int n, unsigned int base) {
    char buf[32];
    int i = 0;
    do { buf[i++] = "0123456789abcdef"[n % base]; n /= base; } while (n);
    while (i) putchar(buf[--i]);
}

void printf(const char *fmt, ...) {
    __builtin_va_list ap;
    __builtin_va_start(ap, fmt);
    while (*fmt) {
        if (*fmt++ != '%') { putchar(fmt[-1]); continue; }
        switch (*fmt) {
        case 'd': case 'i': {
            int n = __builtin_va_arg(ap, int);
            if (n < 0) putchar('-');
            number(n < 0 ? 0u - (unsigned int)n : (unsigned int)n, 10);
            break;
        }
        case 'u': number(__builtin_va_arg(ap, unsigned int), 10); break;
        case 'x': number(__builtin_va_arg(ap, unsigned int), 16); break;
        case 'c': putchar(__builtin_va_arg(ap, int)); break;
        case 's': {
            const char *s = __builtin_va_arg(ap, const char *);
            if (!s) s = "(null)";
            while (*s) putchar(*s++);
            break;
        }
        case '%': putchar('%'); break;
        case 0: __builtin_va_end(ap); return;
        default: putchar('%'); putchar(*fmt); break;
        }
        fmt++;
    }
    __builtin_va_end(ap);
}
