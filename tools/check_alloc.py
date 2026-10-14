#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_alloc.py - 分配返回值检查闸门（生产级审计）

为什么需要：kmalloc/pmm_alloc_page(s) 在失败时返回 NULL/0，调用方不解引用
就行。漏掉检查的表现不是"立刻崩"，而是"内存压力大时偶发写穿 0 地址"，
这类 bug 在测试机上永远绿、真机上偶发。靠人眼 review 48 个调用点会烂掉，
所以做成闸门。

判据（启发式，不是编译器）：
  1. 只扫 .c，先剥注释 —— 注释里写着 "if (!p) return" 不算检查过。
  2. 取调用点左边的变量名。
  3. 在该行之后的 CHECK_WINDOW 行内找判空模式（!p / p == NULL / p == 0 /
     p != NULL / if (!p) / 三元里的 p ? ... : NULL）。
  4. 找不到 -> 报 "unchecked"。

已知会误报的形态（靠白名单 ALLOW 钉住，别放宽判据）：
  - 自检代码故意不检查（要的就是拿到 NULL 后看行为）
  - 分配结果立刻交给一个自己会判空的函数
  - 同一行里已经写了 `if (!p) return` 但被换行拆开

用法：python tools/check_alloc.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KDIR = os.path.join(ROOT, "kernel")

CHECK_WINDOW = 14

# 分配函数 -> (返回值"失败"字面量, 名字)
ALLOCS = {
    "kmalloc": "NULL",
    "pmm_alloc_page": "0",
    "pmm_alloc_pages": "0",
}

# 白名单：(文件, 变量, 行内容特征)。加进来必须写理由。
ALLOW = {
    ("lockselftest.c", "p"): "自检故意拿 NULL 看行为",
}

# 整行白名单：(文件, 行号)。同样必须写理由。
ALLOW_LINE = {
    # 尾调用转发：把 pmm_alloc_pages 的失败值（0）原样交给调用方，
    # 检查责任在调用方，这里再判一次反而是重复。
    ("pmm.c", 54): "return 转发失败值，检查在调用方",
}

CALL_RE = re.compile(r"\b(kmalloc|pmm_alloc_page|pmm_alloc_pages)\s*\(")
# 左边：变量名 = [可选的 cast] 结尾。cast 在 `=` 右边，别写反顺序。
LHS_RE = re.compile(
    r"([A-Za-z_]\w*(?:\s*->\s*\w+|\s*\.\s*\w+|\[[^\]]*\])?)\s*=\s*"
    r"(?:\(\s*[^;]*?\)\s*)?$"
)


def strip_comment_lines(text):
    """剥 // 与 /* */ 注释，保留行号对应关系（用占位空串）。"""
    out = []
    in_block = False
    for ln in text.splitlines():
        s = ln
        if in_block:
            if "*/" in s:
                s = s.split("*/", 1)[1]
                in_block = False
            else:
                out.append("")
                continue
        s = s.split("//")[0]
        while "/*" in s:
            pre = s.split("/*", 1)[0]
            if "*/" in s.split("/*", 1)[1]:
                post = s.split("/*", 1)[1].split("*/", 1)[1]
                s = pre + " " + post
            else:
                s = pre
                in_block = True
        out.append(s)
    return out


def null_patterns(var):
    """判空形态。正负两种都要认：
     * 负形：!p / p == NULL / p == 0（然后 return/goto fail）
     * 正形：if (p) / p && q / p ? ... : ...（然后才用）
     只认负形会把 `if (p) { 用 }` 误报成未检查。"""
    return [
        "!" + var,
        var + " == NULL",
        var + " == 0",
        var + " != NULL",
        var + " != 0",
        var + " == (void *)0",
        "NULL == " + var,
        "0 == " + var,
        "if (" + var + ")",
        "if(" + var + ")",
        var + " ?",
        var + " &&",
        var + " ||",
        "&& " + var,
        "|| " + var,
    ]


def var_base(var):
    """`g_tx_buf[i]` -> `g_tx_buf`；`a->b` -> `a`。"""
    v = var.split("[")[0].strip()
    v = v.split("->")[0].strip()
    v = v.split(".")[0].strip()
    return v


def main():
    bad = []
    total = 0
    for name in sorted(os.listdir(KDIR)):
        if not name.endswith(".c") or name == "kmalloc.c":
            continue
        path = os.path.join(KDIR, name)
        raw = open(path, "r", encoding="utf-8", errors="replace").read()
        lines = strip_comment_lines(raw)
        for i, ln in enumerate(lines):
            m = CALL_RE.search(ln)
            if not m:
                continue
            # 函数定义行（`uint32_t pmm_alloc_page(void) {`）不是调用点
            if ln.rstrip().endswith("{") or ("(" in ln and ")" in ln
                                             and ln.rstrip().endswith("{")):
                continue
            total += 1
            if (name, i + 1) in ALLOW_LINE:
                continue
            # 左边变量名
            left = ln[:m.start()]
            lm = LHS_RE.search(left.rstrip())
            if not lm:
                # 形如 `if (!g_build) g_build = kmalloc(...)` 或纯表达式
                if "if" in left:
                    continue        # 已经被判空包着
                bad.append((name, i + 1, ln.strip(), "<无法解析左值>"))
                continue
            var = lm.group(1).strip()
            vb = var_base(var)
            if not vb:
                continue
            if (name, vb) in ALLOW:
                continue
            # 后续窗口内找判空。同时用带下标的原名（`pages[i] == 0`）和
            # 基名（`!pages`），漏一个就会把已检查的点报成未检查。
            window = "\n".join(lines[i + 1:i + 1 + CHECK_WINDOW])
            # 同一行左侧若已有 `if (!x)` 也算
            same = ln
            found = False
            for cand in (var.replace(" ", ""), vb):
                for pat in null_patterns(cand):
                    if pat in window or pat in same:
                        found = True
                        break
                if found:
                    break
            if not found:
                bad.append((name, i + 1, ln.strip(), vb))

    print("check_alloc: %d alloc call sites, %d unchecked" % (total, len(bad)))
    for f, no, text, v in bad:
        print("  UNCHECKED %s:%d  var=%s  %s" % (f, no, v, text))
    if bad:
        print("-> 逐条复核：真漏检就在代码里加判空；确认安全就 ALLOW 并写理由。")
        return 1
    print("-> ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
