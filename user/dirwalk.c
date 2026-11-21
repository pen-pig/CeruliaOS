/*
 * user/dirwalk.c - 用户态文件系统能力自检（readdir/stat/printf/malloc）
 *
 * 存在的理由：syscall 面补上 SYS_READDIR/SYS_STAT、libc 补上 printf/malloc
 * 之后，用户态程序第一次有能力"发现文件系统里有什么、再决定读哪个"——
 * 这是写实际应用（而不是测试桩）的最低门槛。本程序把四件新能力各跑一遍
 * 正反例，全部打到 stdout，由 tests/test_userfs.py 逐行断言。
 *
 * 用法：dirwalk [extra_path]
 *   extra_path：额外 stat 一个路径（E2E 先在内核 shell 里造好文件，
 *   再让本程序 stat 它，顺带覆盖"数据盘后端"而不只是内置系统卷）。
 *
 * 任何一项不符预期：打印 FAIL 行并返回 1；全过返回 0。
 */
#include "libc.h"

static int fails = 0;

static void check(int ok, const char *label) {
    printf("userfs: %s %s\n", ok ? "PASS" : "FAIL", label);
    if (!ok) fails++;
}

int umain(int argc, char **argv) {
    printf("userfs: dirwalk start (argc=%d)\n", argc);

    /* ---- 1) printf 全规格 ---- */
    printf("userfs: printf d=%d i=%i u=%u x=%x X=%X c=%c s=%s pct=%%\n",
           -42, -42, 42u, 42u, 42u, 'C', "str");
    check(1, "printf emitted all specifiers");

    /* ---- 2) malloc/free：不重叠 + 合并后原地重用 ----
     * a 先 b 后，两者物理相邻；free 两块的顺序触发地址序合并，
     * 之后 malloc(100) 必须拿回 a 的地址（first-fit + 合并生效）。 */
    char *a = (char *)malloc(100);
    char *b = (char *)malloc(200);
    check(a != 0 && b != 0, "malloc two blocks");
    if (a && b) {
        for (int i = 0; i < 100; i++) a[i] = (char)0x5A;
        for (int i = 0; i < 200; i++) b[i] = (char)0xA5;
        int ok = 1;
        for (int i = 0; i < 100; i++) if ((unsigned char)a[i] != 0x5A) ok = 0;
        for (int i = 0; i < 200; i++) if ((unsigned char)b[i] != 0xA5) ok = 0;
        check(ok, "blocks do not overlap");
        free(a);
        free(b);
        char *c = (char *)malloc(100);
        check(c == a, "freed block reused in place (coalesce)");
        free(c);
    }
    check(malloc(0xFFFFFFFFu) == 0, "malloc(huge) rejected");

    /* 二次释放：libc 应打一行 stderr 并不弄坏堆。之后再分配必须照常工作。 */
    a = (char *)malloc(64);
    free(a);
    free(a);                       /* 第二次：应被拒 */
    char *d = (char *)malloc(64);
    check(d != 0, "heap healthy after double-free rejected");
    free(d);

    /* ---- 3) readdir /bin（内置系统卷，不依赖任何盘） ---- */
    static user_dirent_t ents[32];
    int n = readdir("/bin", ents, 32);
    printf("userfs: readdir /bin -> %d entries\n", n);
    check(n > 0, "readdir /bin returns entries");
    int saw_hello = 0;
    for (int i = 0; i < n; i++) {
        printf("userfs:   [%d] %s size=%u dir=%u\n",
               i, ents[i].name, ents[i].size, (unsigned)ents[i].is_dir);
        if (strcmp(ents[i].name, "hello.elf") == 0 && ents[i].size > 0 &&
            ents[i].is_dir == 0) saw_hello = 1;
    }
    check(saw_hello, "hello.elf listed with size and not-dir");
    check(readdir("/nonexistent-dir", ents, 32) == -1,
          "readdir missing dir rejected");
    check(readdir("/bin/hello.elf", ents, 32) == -1,
          "readdir on a file rejected");

    /* ---- 4) stat ---- */
    user_stat_t st;
    int r = stat("/bin/hello.elf", &st);
    printf("userfs: stat /bin/hello.elf -> rc=%d size=%u dir=%u\n",
           r, st.size, (unsigned)st.is_dir);
    check(r == 0 && st.size > 0 && st.is_dir == 0, "stat file gives size");

    r = stat("/bin", &st);
    printf("userfs: stat /bin -> rc=%d dir=%u\n", r, (unsigned)st.is_dir);
    check(r == 0 && st.is_dir == 1, "stat /bin says directory");

    check(stat("/nonexistent-file", &st) == -1, "stat missing rejected");

    /* ---- 5) 可选：stat E2E 事先造好的数据盘文件 ---- */
    if (argc > 1 && argv[1] != 0 && argv[1][0] != 0) {
        r = stat(argv[1], &st);
        printf("userfs: stat %s -> rc=%d size=%u dir=%u\n",
               argv[1], r, st.size, (unsigned)st.is_dir);
        check(r == 0 && st.size == 8u && st.is_dir == 0,
              "stat data-disk file gives written size");
    }

    printf("userfs: %s (%d failures)\n", fails ? "FAILED" : "ALL OK", fails);
    return fails ? 1 : 0;
}
