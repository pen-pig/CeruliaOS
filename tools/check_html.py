# -*- coding: utf-8 -*-
"""check_html.py - HTML 解析/断行闸门（run_tests.py static 层入口）

真正的对拍逻辑在 tests/test_html_probe.py：它把生产用的
`rust/ezos_rs/src/html.rs` 编成宿主探针，与 tests/ref_html.py 那份
**独立**的 Python 实现逐字节比。

为什么要一个壳：run_tests.py 按名字找脚本，`html` 会去找
tests/test_html.py / tools/html.py / tools/check_html.py。名字对不上就
静默变成"找不到脚本"，而不是"没跑"——所以这里显式指过去。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import test_html_probe  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(test_html_probe.main())