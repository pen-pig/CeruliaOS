/*
 * user/libc.h - 用户态极简 libc（用户态生态 ③）
 *
 * 这是教学内核的用户态"标准库"最小集，目标是把 5 个用户程序里重复手抄的
 * 系统调用封装和串/内存小工具收拢到一处，链接时用 `user/libc.o` 复用
 * （每个程序各自独立链接 user/user.ld，freestanding）。
 *
 * 设计边界：
 *   - 真正的陷入（int 0x80）由 crt0.asm 提供 write/read/open/close/lseek/
 *     _exit/sockcall 这些符号；libc 直接声明它们为 extern 并复用，不重复实现。
 *   - fork/waitpid/pipe/getpid 内核没有对应的 crt0 包装，由 libc 自己用内联
 *     汇编陷入（与 forktest 之前的写法一致）。
 *   - 不引入任何标准头（freestanding），只用编译器内建类型。
 */
#ifndef LIBC_H
#define LIBC_H

/* ---- open 的 flags：必须与内核 fd.h 逐位一致 ----
 * 访问模式（O_RDONLY/O_WRONLY/O_RDWR）+ 可组合的创建/截断/追加位。
 * 以前这里只定义了 0/1/2 三个值，写文件靠"O_WRONLY 隐含创建+截断"，
 * 既不标准也让调用方无法表达"只写但别截断"。 */
#define O_RDONLY  0
#define O_WRONLY  1
#define O_RDWR    2
#define O_CREAT   0x40
#define O_EXCL    0x80
#define O_TRUNC   0x200
#define O_APPEND  0x400

/* lseek 的 whence */
#define SEEK_SET  0
#define SEEK_CUR  1
#define SEEK_END  2

/* ---- crt0.asm 提供的基础系统调用封装（链接时由 crt0.o 解析） ---- */
int write(int fd, const void *buf, unsigned int n);
int read(int fd, void *buf, unsigned int n);
int open(const char *name, int flags);
int close(int fd);
int lseek(int fd, int off, int whence);
void _exit(int code) __attribute__((noreturn));
int sockcall(int subcmd, void *args);

/* ---- 目录项 / 属性结构：布局必须与内核逐字节一致 ----
 * readdir 的输出缓冲由内核校验后**直写**（syscall.c SYS_READDIR 注释），
 * 所以这里差一个字节，内核写的就是别的字段。用 _Static_assert 钉住，
 * 布局漂移在编译期炸，而不是在 E2E 里变成乱码名字。 */
typedef struct {
    char          name[256];    /* NUL 结尾；内核侧 fs_dir_entry_t.name */
    unsigned int  size;         /* 字节数；目录项为 0 */
    unsigned char is_dir;       /* 1=目录 */
} user_dirent_t;

typedef struct {
    unsigned int  size;         /* +0：内核按小端四字节写回 */
    unsigned char is_dir;       /* +4 */
} user_stat_t;                  /* sizeof=8（含对齐填充），内核写 5 字节 */

_Static_assert(sizeof(user_dirent_t) == 264,
               "user_dirent_t must match kernel fs_dir_entry_t");
_Static_assert(__builtin_offsetof(user_dirent_t, size) == 256, "size offset drift");
_Static_assert(__builtin_offsetof(user_dirent_t, is_dir) == 260, "is_dir offset drift");
_Static_assert(sizeof(user_stat_t) == 8, "user_stat_t must match kernel writeback");

/* readdir：一次性列完一个目录（SYS_READDIR=46）。返回条目数
 * （0=空目录），失败 -1。ents 见上面的 user_dirent_t。 */
int readdir(const char *path, user_dirent_t *ents, unsigned int max);
/* stat：取路径属性（SYS_STAT=47）。存在写回 {size,is_dir} 返回 0，
 * 不存在/未挂载 -1。 */
int stat(const char *path, user_stat_t *st);

/* ---- libc 提供的系统调用薄封装（int 0x80 直接陷入） ---- */
int  syscall(int num, int a, int b, int c);
void exit(int code) __attribute__((noreturn));
int  fork(void);                       /* 返回：父=子pid，子=0，失败=-1 */
int  waitpid(int pid, int *status, int options);
int  pipe(int fds[2]);                 /* fds[0]=读, fds[1]=写 */
int  getpid(void);

/* ---- 字符串 / 内存 ---- */
unsigned int strlen(const char *s);
int          strcmp(const char *a, const char *b);
int          strncmp(const char *a, const char *b, unsigned int n);
void        *memcpy(void *dst, const void *src, unsigned int n);
void        *memset(void *dst, int v, unsigned int n);
void         itoa(unsigned int v, char *out);   /* 十进制，NUL 结尾 */
void         puts(const char *s);               /* 原样写出，不加换行 */
void         putdec(int v);                     /* 十进制打印（支持负数） */

/* ---- 格式化输出（%d %i %u %x %X %c %s %%，无宽度/精度）---- */
void         printf(const char *fmt, ...);

/* ---- 堆：静态池 + free-list（见 libc.c 顶部的设计注释）---- */
void        *malloc(unsigned int n);
void         free(void *p);

/* ---- 系统调用号（与内核 syscall.h 对齐；只封装内核已实现的） ---- */
#define SYS_READ    0u
#define SYS_WRITE   1u
#define SYS_EXIT    2u
#define SYS_OPEN    5u
#define SYS_CLOSE   6u
#define SYS_LSEEK   19u
#define SYS_PIPE    42u
#define SYS_FORK    43u
#define SYS_WAITPID 44u
#define SYS_GETPID  45u
#define SYS_READDIR 46u
#define SYS_STAT    47u
#define WNOHANG     1        /* 非阻塞 waitpid：子进程仍在跑则返回 0（不收割） */

#endif
