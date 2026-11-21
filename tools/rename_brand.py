# -*- coding: utf-8 -*-
"""一次性改名脚本：把对外显示名 EZOS 改成 Cerulia。

边界（故意不动）：
  - EZOS_VERSION / EZOSREFS / EZOSPING / EZOS_SLOT 等标识符（\bEZOS\b 天然跳过）
  - 含 "pipe selftest" 的行：那是 selftest 的测试数据，不是品牌文本
磁盘格式魔数 "EZOSREFS" 必须保留，否则已格式化的 ReFS 卷认不出来。
"""
import re
import sys
from pathlib import Path

PAT = re.compile(r"\bEZOS\b")
NEW = "Cerulia"


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "kernel")
    if len(sys.argv) > 2:
        exts = set(sys.argv[2].split(","))
    else:
        exts = {".c", ".h"}
    total = 0
    files = 0
    for p in sorted(root.rglob("*")):
        if p.suffix not in exts or not p.is_file():
            continue
        raw = p.read_bytes()
        try:
            txt = raw.decode("utf-8")
        except UnicodeDecodeError:
            print("skip (not utf-8): %s" % p)
            continue
        if "EZOS" not in txt:
            continue
        lines = txt.split("\n")
        n = 0
        for i, line in enumerate(lines):
            if "pipe selftest" in line:
                continue
            lines[i], k = PAT.subn(NEW, line)
            n += k
        if n:
            p.write_bytes("\n".join(lines).encode("utf-8"))
            print("%-28s %d" % (p, n))
            total += n
            files += 1
    print("---- files=%d replacements=%d" % (files, total))


if __name__ == "__main__":
    main()
