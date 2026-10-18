/*
 * user/libc.c - 用户态极简 libc 实现（用户态生态 ③）
 *
 * freestanding：不链接任何标准库，只用编译器内建类型 + crt0.asm 提供的
 * write/read/open/close/lseek/_exit/sockcall。本文件实现：
 *   - 通用陷入 syscall() 与 fork/waitpid/pipe/getpid/exit 薄封装
 *   - 字符串/内存小工具：strlen/strcmp/strncmp/memcpy/memset/itoa
 *   - 终端打印辅助：puts / putdec
 *
 * 所有函数保持最小且跨程序行为一致，方便把 helloworld 类程序里手抄的
 * kstrlen/kputs/kputdec 收拢到这里而不改变任何一行屏幕输出。
 */
#include "libc.h"

/* ---- 系统调用薄封装 ---- */

int syscall(int num, int a, int b, int c) {
    int r;
    __asm__ volatile("int $0x80"
                     : "=a"(r)
                     : "a"(num), "b"(a), "c"(b), "d"(c)
                     : "memory");
    return r;
}

void exit(int code) {
    _exit(code);          /* crt0.asm 的 _exit 走 SYS_EXIT，永不返回 */
}

int fork(void) {
    int r;
    __asm__ volatile("int $0x80" : "=a"(r) : "a"(SYS_FORK) : "memory");
    return r;
}

int waitpid(int pid, int *status, int options) {
    int r;
    /* ebx=pid, ecx=status 用户指针, edx=options。
     * 内核返回：>=0 退出码（已回收，*status 写入退出码，返回被等 pid）；
     *           0 表示 WNOHANG 且子进程仍在跑（未回收）；
     *           -1 无效 pid / 已被收走。
     * 这里原样透传，调用方按"成功返回被等 pid"判定即可（与旧 kwaitpid 一致）。 */
    __asm__ volatile("int $0x80"
                     : "=a"(r)
                     : "a"(SYS_WAITPID), "b"(pid), "c"(status), "d"(options)
                     : "memory");
    return r;
}

int pipe(int fds[2]) {
    int r;
    __asm__ volatile("int $0x80"
                     : "=a"(r)
                     : "a"(SYS_PIPE), "b"(fds)
                     : "memory");
    return r;
}

int getpid(void) {
    int r;
    __asm__ volatile("int $0x80" : "=a"(r) : "a"(SYS_GETPID) : "memory");
    return r;
}

/* ---- 字符串 / 内存 ---- */

unsigned int strlen(const char *s) {
    unsigned int n = 0;
    while (s && s[n]) n++;
    return n;
}

int strcmp(const char *a, const char *b) {
    if (a == 0 && b == 0) return 0;
    if (a == 0) return -1;
    if (b == 0) return 1;
    while (*a && *a == *b) { a++; b++; }
    return (int)(unsigned char)*a - (int)(unsigned char)*b;
}

int strncmp(const char *a, const char *b, unsigned int n) {
    if (n == 0) return 0;
    while (n-- && *a && *a == *b) { a++; b++; }
    return (int)(unsigned char)*a - (int)(unsigned char)*b;
}

void *memcpy(void *dst, const void *src, unsigned int n) {
    unsigned char *d = (unsigned char *)dst;
    const unsigned char *s = (const unsigned char *)src;
    for (unsigned int i = 0; i < n; i++) d[i] = s[i];
    return dst;
}

void *memset(void *dst, int v, unsigned int n) {
    unsigned char *d = (unsigned char *)dst;
    unsigned char c = (unsigned char)v;
    for (unsigned int i = 0; i < n; i++) d[i] = c;
    return dst;
}

void itoa(unsigned int v, char *out) {
    char b[12];
    int n = 0;
    if (v == 0) b[n++] = '0';
    while (v) { b[n++] = (char)('0' + (v % 10u)); v /= 10u; }
    int m = 0;
    while (n) out[m++] = b[--n];
    out[m] = '\0';
}

/* ---- 终端打印辅助 ---- */

void puts(const char *s) {
    write(1, s, strlen(s));
}

/* 十进制打印（正数/0/负数都支持，负数带 '-'），逐字符写出，
 * 与旧 kputdec 输出完全一致。 */
void putdec(int v) {
    unsigned u = (v < 0) ? (unsigned)(-v) : (unsigned)v;
    char b[12];
    int n = 0;
    if (v < 0) write(1, "-", 1);
    if (u == 0) b[n++] = '0';
    while (u) { b[n++] = (char)('0' + (u % 10u)); u /= 10u; }
    while (n) { char c = b[--n]; write(1, &c, 1); }
}

/* ---- 格式化输出 ---- */
/*
 * printf：最小可用集——%d %i %u %x %X %c %s %%。没有宽度/精度/对齐，
 * 用不上就不背（freestanding 每多一个分支都是镜像体积）。逐字符走
 * write 系统调用：演示程序一次打印几十字节，慢不到哪去，换来的是
 * 零静态缓冲、任何调用深度下都不会踩自己的栈。
 */
static void pr_char(char c) { write(1, &c, 1); }

static void pr_str(const char *s) {
    if (s == 0) s = "(null)";
    write(1, s, strlen(s));
}

static void pr_uint(unsigned int v, unsigned int base, int upper) {
    static const char lo[] = "0123456789abcdef";
    static const char hi[] = "0123456789ABCDEF";
    const char *digits = upper ? hi : lo;
    char b[10];                       /* 32 位任意进制最多 10 位（2 进制 32，这里只到 16） */
    int n = 0;
    if (v == 0) b[n++] = '0';
    while (v) { b[n++] = digits[v % base]; v /= base; }
    while (n) { char c = b[--n]; write(1, &c, 1); }
}

void printf(const char *fmt, ...) {
    __builtin_va_list ap;
    __builtin_va_start(ap, fmt);
    for (const char *p = fmt; *p; p++) {
        if (*p != '%') { pr_char(*p); continue; }
        p++;
        switch (*p) {
        case 'd': case 'i': {
            int v = __builtin_va_arg(ap, int);
            if (v < 0) { pr_char('-'); pr_uint((unsigned)(-v), 10u, 0); }
            else pr_uint((unsigned)v, 10u, 0);
            break;
        }
        case 'u': pr_uint(__builtin_va_arg(ap, unsigned int), 10u, 0); break;
        case 'x': pr_uint(__builtin_va_arg(ap, unsigned int), 16u, 0); break;
        case 'X': pr_uint(__builtin_va_arg(ap, unsigned int), 16u, 1); break;
        case 'c': pr_char((char)__builtin_va_arg(ap, int)); break;
        case 's': pr_str(__builtin_va_arg(ap, const char *)); break;
        case '%': pr_char('%'); break;
        case '\0': pr_char('%'); p--; break;    /* fmt 以孤零 '%' 结尾 */
        default:   pr_char('%'); pr_char(*p); break;
        }
    }
    __builtin_va_end(ap);
}

/* ---- 堆：静态池 + free-list ----
 *
 * 设计约束与选择理由：
 *  - 池是 .bss 里的定长数组，不向内核要内存。用户程序各自有独立地址
 *    空间（exec/fork 按页复制），池天然随进程私有，不需要任何锁。
 *  - 首次 malloc 时懒初始化整池为一个大空闲块。不在 crt0 里挂钩子：
 *    不堆内存的程序（hello/spin）不该为用不到的东西付初始化成本。
 *  - 空闲链按**地址有序**，free 时与物理相邻块合并——否则
 *    "alloc/free 交替"几下就把池子切成大量无法重用的小块。
 *  - 块头 magic 双向判定：**空闲块=HEAP_MAGIC，占用块=0**。free 时只接受
 *    "magic==0 且在池内 4 对齐"的指针——二次释放（magic 已是 HEAP_MAGIC）
 *    和野指针都打一行 stderr 后忽略。早期版本占用块也留 HEAP_MAGIC，二次
 *    释放会把同一块插进空闲链两次，链上出现环，后续 malloc 直接绕死。
 */
#define HEAP_SIZE  32768u                  /* 32KB：demo 够用，.bss 不心疼 */
#define HEAP_MAGIC 0x48454150u             /* "HEAP"；占用块置 0 */

typedef struct blk {
    unsigned int  size;                    /* 数据区字节数（不含头部） */
    unsigned int  magic;                   /* 空闲=HEAP_MAGIC，占用=0 */
    struct blk   *next;                    /* 仅空闲块有意义：地址序下一个 */
} blk_t;

static unsigned char heap[HEAP_SIZE];
static blk_t *heap_free;
static int heap_ready = 0;

#define BLK_HDR ((unsigned int)sizeof(blk_t))

static void heap_init(void) {
    blk_t *b = (blk_t *)heap;
    b->size = HEAP_SIZE - BLK_HDR;
    b->magic = HEAP_MAGIC;
    b->next = 0;
    heap_free = b;
    heap_ready = 1;
}

static void heap_note(const char *what) {
    write(2, "libc: ", 6);
    write(2, what, strlen(what));
    write(2, "\n", 1);
}

void *malloc(unsigned int n) {
    if (n == 0) return 0;
    /* 上界检查必须在**对齐加法之前**：n=0xFFFFFFFF 时 (n+3) 回绕成 2，
     * 对齐后变 0，越过大小检查返回一个 0 字节"块"——本项目经典事故形态
     * （内核侧 size=0xFFFFFFF0 回绕过一次），dirwalk 的 malloc(huge)
     * 反例当场抓到过这个 bug。 */
    if (n > HEAP_SIZE - BLK_HDR) return 0;
    if (!heap_ready) heap_init();
    /* 4 字节对齐：块头 12 字节 + 池 4 对齐，数据区保持对齐 */
    n = (n + 3u) & ~3u;

    blk_t *prev = 0;
    for (blk_t *b = heap_free; b; prev = b, b = b->next) {
        if (b->size < n) continue;
        /* 剩余部分放得下一个头 + 至少 4 字节数据才分割，否则整块给出 */
        if (b->size >= n + BLK_HDR + 4u) {
            blk_t *r = (blk_t *)((unsigned char *)b + BLK_HDR + n);
            r->size = b->size - n - BLK_HDR;
            r->magic = HEAP_MAGIC;
            r->next = b->next;
            b->size = n;
            if (prev) prev->next = r; else heap_free = r;
        } else {
            if (prev) prev->next = b->next; else heap_free = b->next;
        }
        b->magic = 0;                      /* 占用标记：free 据此识别二次释放 */
        return (unsigned char *)b + BLK_HDR;
    }
    return 0;                              /* 池耗尽 */
}

void free(void *p) {
    if (p == 0) return;
    blk_t *b = (blk_t *)((unsigned char *)p - BLK_HDR);
    if ((unsigned char *)b < heap ||
        (unsigned char *)b + BLK_HDR > heap + HEAP_SIZE ||
        ((unsigned int)(unsigned char *)p & 3u) != 0 ||
        b->magic != 0) {                   /* 非 0 = 野指针或二次释放 */
        heap_note("free: bad pointer or double free, ignored");
        return;
    }
    b->magic = HEAP_MAGIC;
    b->next = 0;

    /* 地址序插入 + 与前后相邻空闲块合并 */
    blk_t *prev = 0;
    blk_t *cur = heap_free;
    while (cur && cur < b) { prev = cur; cur = cur->next; }
    b->next = cur;
    if (prev) prev->next = b; else heap_free = b;

    if (b->next &&
        (unsigned char *)b + BLK_HDR + b->size == (unsigned char *)b->next) {
        b->size += BLK_HDR + b->next->size;
        b->next = b->next->next;
    }
    if (prev &&
        (unsigned char *)prev + BLK_HDR + prev->size == (unsigned char *)b) {
        prev->size += BLK_HDR + b->size;
        prev->next = b->next;
    }
}
