# 品牌命名约定

## 当前系统名

**Cerulia** — 读作 /səˈruːliə/，中文**「蔚蓝」**。

词源：拉丁 *caeruleus*（天蓝色）+ 名词后缀 *-ia*。自造词，全拼搜索基本空白，
域名与账号可抢注。2026-10-17 由 **EZOS** 改名而来。

被否决的候选与原因（避免以后重复提议）：

| 候选 | 否决原因 |
|---|---|
| Selene | Fedora spin + 一个 Windows 安全项目 |
| Vesper / Nocturne / Halcyon / Nyx | 均已有同名 OS 项目（NyxOS、NocturneOS 等） |
| Caelum | Pyxis OS 的内核名，另有 Android 内核同名 |
| Pallas | JAX 的内核编程扩展语言 |
| Sparse | Linux 的静态分析器；且 "sparse file" 是文件系统术语 |
| Twilight | 《暮光之城》IP + Rust `twilight` 库 + 已有一个 Twilight OS（含 TwilightFS） |
| Dusk | 已有一个 Forth 实现的 Dusk OS |
| Zephyr / Aurora / Nova / Serenity / Eos | 老面孔，占用严重 |

## 改名边界：这些**不能**动

品牌名改了，但下面这些是**磁盘格式 / 协议 / 内部标识**，改了会让已有的盘认不出来：

| 标识 | 位置 | 为什么不动 |
|---|---|---|
| `EZOSREFS` | `kernel/refs.c` 卷头魔数 `0x455A4F5352454653` | ReFS 卷的自标识，改了旧盘无法挂载 |
| `EZOSPING` | `kernel/net.c` ICMP payload | 协议侧标识 |
| `EZOS_VERSION` | `kernel/version.h` 宏名 | 内部标识符（`\bEZOS\b` 天然跳过） |
| `EZOS_SLOT` / `EZOS_TOOLS` / `EZOS_PYTHON` / `EZOS_NINJA` | 测试与构建脚本 | 环境变量名 |
| `ezos_rs` / `ezos_zig` / `libezos_rs.a` | `rust/` | 目录与产物名，改了构建链要跟着动 |
| `"EZOS pipe selftest 0123456789"` | `kernel/shell_extra.c` | selftest 测试数据，长度参与断言 |
| 数据盘 BPB 的 OEM 标签 `EZOS       ` | `tools/gen_diskimg.py` | 磁盘格式字段（11 字节） |

## 怎么改

```
python tools/rename_brand.py <目录> <扩展名>     # 例：kernel .c,.h
```

脚本用 `\bEZOS\b` 匹配，因此 `EZOS_VERSION`、`EZOSREFS` 这类带下划线或连写的
标识符自动跳过；含 `pipe selftest` 的行也跳过。

改完必做（顺序不能反）：

1. `python tools/make_sysvol.py` —— sysvol 里的文案也要跟着变
2. `./ninja.exe -f build_auto.ninja`
3. 至少跑：`python tools/run_tests.py static uefi3 regress path power netapp`

## 踩过的坑（改名前必读）

- **测试别硬编码品牌名**。`test_uefi3.py` 曾断言 `"EZOS Shell"`、`test_netapp.py`
  曾断言 `b"EZOS"`、`test_path.py` 曾断言 sysvol 里的 `"EZOS built-in system volume"`。
  现在都已改成钉**句式/契约**：`Shell - Type 'help'`、`<h1>`+`i686`、`built-in system volume`。
- **别硬编码文件字节数**。`kernel/fd.c` 的 `fd_selftest` 曾断言 README.TXT 恰好 46 字节，
  文案从 EZOS 改成 Cerulia 后变 49 字节 → fd 自检 FAIL → 开机自检 `SYSTEM UNSTABLE`
  → uefi3 的 OCR 拿不到 `ALL PASS`，整个用例红。现在改为钉契约（以 `Welcome ` 开头、
  `lseek END` 与读回长度自洽）。
- GUI 里按字符数算好的居中要跟着改（启动标题 `cx - 16` → `cx - 28`，7 字符 × 8px / 2）。
