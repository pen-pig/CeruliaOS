# 在 Cerulia 内编译 C

启动到命令行后，用 `vi demo.c` 新建源码。按 `i` 输入以下程序，再按 `Esc`、输入 `:wq` 并回车保存。

```c
int sum(int n) {
    int total = 0;
    while (n > 0) {
        total = total + n;
        n = n - 1;
    }
    return total;
}

int main(void) {
    printf("sum=%d\n", sum(5));
    return 0;
}
```

在 Cerulia 命令行中执行：

```text
exec cc demo.c demo.elf
exec demo.elf
```

屏幕会显示 `sum=15`。源码和 ELF 都保存在数据盘，下次启动可重新编辑或运行。编译阶段报错时，同名旧 ELF 会保留。

首版面向单个 `.c` 文件，支持 `int`、函数、局部变量初始化、条件、循环、字符和字符串常量。运行时提供 `puts`、`putchar`、`printf`（`%d`、`%i`、`%u`、`%x`、`%c`、`%s`、`%%`），以及 Cerulia 的 `read`、`write`、`open`、`close`、`exit`。暂未实现头文件、宏、结构体、浮点和多文件链接。源码上限为 16 KiB（不含），编译器的符号和代码缓冲另有上限。

编译器移植自 Fabrice Bellard 的 [Obfuscated Tiny C Compiler](https://bellard.org/otcc/)。Cerulia 版在用户态生成静态 i386 ELF，使用系统内置的最小运行时。原始许可保留在编译器源码中。
