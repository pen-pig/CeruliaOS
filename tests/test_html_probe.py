# -*- coding: utf-8 -*-
"""test_html_probe.py - HTML 解析/断行：生产 Rust 实现 vs 独立 Python 参考实现

**这个测试要防的是什么**

tests/ref_html.py 是人写的第二份实现。它自己对拍自己变绿，说明不了任何
事 —— 两边可能一起错。所以这里把对拍的另一端换成**生产代码本身**：

    tests/probe/html_probe.rs  --[#[path] 直接 include]-->  rust/ezos_rs/src/html.rs

一行都不改、不复制。跑的是内核里那个 `html_parse_rs` / `utf8_wrap_rs`。

没装 rustc 时整个文件 SKIP（不是 FAIL）：宿主探针是开发期闸门，
CI 里 rustc 由 ezos_env.py 找到，找不到就跳过而不是让构建红掉。

跑法：
    python tests/test_html_probe.py
    python tests/test_html_probe.py -v      # 打印每条用例
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ref_html as R  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROBE_RS = os.path.join(HERE, "probe", "html_probe.rs")
PROBE_EXE = os.path.join(ROOT, "temp", "html_probe.exe")


def _find_rustc():
    """找一个能编 i686/宿主 Rust 的 rustc。找不到返回 None（走 SKIP）。"""
    cand = []
    env = os.environ.get("EZOS_CARGO")
    if env:
        cand.append(os.path.join(os.path.dirname(env), "rustc.exe"))
        cand.append(os.path.join(os.path.dirname(env), "rustc"))
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    cand.append(os.path.join(home, ".cargo", "bin", "rustc.exe"))
    cand.append(os.path.join(home, ".cargo", "bin", "rustc"))
    for c in cand:
        if c and os.path.isfile(c):
            return c
    from shutil import which
    return which("rustc") or which("rustc.exe")


def _sync_samples():
    """内核里的 HTML_SAMPLES 必须与 ref_html.py 的 CASES 逐字节一致。

    内建样本（webview.c 的 HTML_SAMPLES）存在的原因是 QMP sendkey 打不出
    '<' '>'，E2E 没法把 HTML 敲进 guest。代价是**同一批样本出现在两个地方**
    （内核 C 字符串 + 参考实现），必然会漂 —— 漂了以后 guest 的输出看着
    正常，实际上已经不在测同一批输入。所以这里把它当闸门钉住。
    """
    fails = []
    src = os.path.join(ROOT, "kernel", "webview.c")
    with open(src, "rb") as f:
        blob = f.read().decode("utf-8", "replace")
    m = re.search(r"HTML_SAMPLES\[\]\s*=\s*\{(.*?)\n\};", blob, re.S)
    if not m:
        return ["webview.c 里找不到 HTML_SAMPLES 数组（改名/挪位了？）"]
    # 抓所有 "..." 字面量并按 C 规则解转义
    lits = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
    kernel_samples = [_c_unescape(x) for x in lits]

    # **按名字取，不按位置取**：CASES 的插入顺序跟样本编号无关，早先按
    # names[:5] 取结果整体错位一格（拿 space 去比 head），报出来的差异
    # 看着像"解析器把标题当空白折叠了"，能把人带偏很久。
    names = ["entity", "head", "link", "style", "script"]
    want = [R.CASES[n][0] for n in names]
    # 样本 6 是 ref_html 里 utf8 + br 两条的合并
    want.append(R.CASES["utf8"][0][:-len(b"</p>")] + b"<br>line2</p>")
    want = [w if isinstance(w, bytes) else w.encode("utf-8") for w in want]

    for i, (k, w) in enumerate(zip(kernel_samples, want), 1):
        if k != w:
            fails.append("样本 %d 与 ref_html 不一致\n"
                         "    kernel=%r\n    ref   =%r" % (i, k, w))
    if len(kernel_samples) != len(want):
        fails.append("样本条数不一致 kernel=%d ref=%d"
                     % (len(kernel_samples), len(want)))
    return fails


def _c_unescape(s):
    """把 C 字符串字面量解成字节（只处理本文件实际用到的那几种转义）"""
    out = bytearray()
    i = 0
    while i < len(s):
        c = s[i]
        if c != "\\":
            out.extend(c.encode("utf-8"))
            i += 1
            continue
        i += 1
        if i >= len(s):
            break
        d = s[i]
        if d == "x":
            j = i + 1
            h = ""
            while j < len(s) and len(h) < 2 and s[j] in "0123456789abcdefABCDEF":
                h += s[j]
                j += 1
            out.append(int(h, 16))
            i = j
        elif d == "n":
            out.append(0x0A)
            i += 1
        elif d == "t":
            out.append(0x09)
            i += 1
        elif d == "\\":
            out.append(0x5C)
            i += 1
        elif d == '"':
            out.append(0x22)
            i += 1
        else:
            out.extend(d.encode("utf-8"))
            i += 1
    return bytes(out)


def _find_nm():
    """找一个能读 .a 的 nm。找不到返回 None（那一项闸门跳过）。"""
    from shutil import which
    for c in ("i686-elf-nm", "i686-elf-nm.exe", "nm", "nm.exe"):
        p = which(c)
        if p:
            return p
    try:
        sys.path.insert(0, ROOT)
        import ezos_env
        return ezos_env.tool("nm")
    except Exception:                                # noqa: BLE001
        return None


def build_probe(rustc):
    os.makedirs(os.path.dirname(PROBE_EXE), exist_ok=True)
    # 探针 include 的是生产 html.rs，改了它就重编（mtime 比较）
    src_m = os.path.getmtime(PROBE_RS)
    html_m = os.path.getmtime(os.path.join(ROOT, "rust", "ezos_rs",
                                           "src", "html.rs"))
    if (os.path.isfile(PROBE_EXE)
            and os.path.getmtime(PROBE_EXE) > max(src_m, html_m)):
        return True
    p = subprocess.run([rustc, "--edition", "2021", "-O", "-o", PROBE_EXE,
                        PROBE_RS],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[-4000:])
        return False
    return True


def _run(mode, payload_hex, extra=None):
    cmd = [PROBE_EXE, mode]
    # **判 None 而不是判真值**：cols=0 是一个必须真正传到被测端的
    # 边界输入，用 `if extra:` 会把它当成"没传"而退回默认 80，
    # 然后报出一条"Rust 和 Python 不一致"的假差异。
    if extra is not None:
        cmd.append(str(extra))
    p = subprocess.run(cmd, input=payload_hex, capture_output=True,
                       text=True)
    if p.returncode != 0:
        raise RuntimeError("probe failed: %s" % p.stderr[-2000:])
    return p.stdout.splitlines()


def probe_parse(html):
    """-> (nodes, stats, title)；nodes 与 ref_html.Node 同构"""
    src = html if isinstance(html, bytes) else html.encode("utf-8")
    lines = _run("parse", src.hex())
    nodes = []
    stats = None
    title = b""
    for ln in lines:
        f = ln.split(" ", 1)
        if f[0] == "n":
            v = ln.split(" ")
            # n kind tag style level len hrefoff texthex
            nodes.append(R.Node(kind=int(v[1]), tag=int(v[2]),
                                style=int(v[3]), level=int(v[4]),
                                text_off=0, text_len=int(v[5]),
                                href_off=int(v[6])))
            # 正文**用探针自己吐出来的字节**。不能拿 py 的竞技场去索引
            # rust 的偏移 —— 两边缓冲不同，偏移毫无可比性。
            nodes[-1].body = bytes.fromhex(v[7]) if len(v) > 7 else b""
        elif f[0] == "stats":
            v = [int(x) for x in ln.split(" ")[1:]]
            stats = v
        elif f[0] == "title":
            title = bytes.fromhex(ln.split(" ", 1)[1]) if len(f) > 1 else b""
    return nodes, stats, title


def probe_wrap(data, cols):
    lines = _run("wrap", data.hex(), extra=cols)
    out = []
    for ln in lines:
        if ln.startswith("line "):
            h = ln[5:]
            out.append(bytes.fromhex(h) if h else b"")
    return out


def probe_width(data):
    return int(_run("width", data.hex())[0].strip())


# ------------------------------------------------------------------
# 补充向量：故意挑"人写的两份实现最容易分叉"的输入
# ------------------------------------------------------------------
EXTRA_PARSE = {
    # 嵌套样式必须能一层层脱掉：<b><i>x</b></i> 里 x 带 BOLD|ITALIC，
    # </b> 之后回到只剩 ITALIC —— 恢复顺序错了这里就露馅
    "nest": b"<b><i>x</i>y</b><u>z</u>",
    # 属性值里的实体（href="?a=1&amp;b=2"）
    "attr_ent": b'<a href="?a=1&amp;b=2">t</a>',
    # 无引号属性值
    "attr_noq": b'<a href=/x.html>t</a>',
    # 单引号属性值
    "attr_sq": b"<a href='/y.html'>t</a>",
    # 属性名大小写混写
    "attr_case": b'<a HREF="/z.html">t</a>',
    # <img> 没有 alt：不该产出节点
    "img_noalt": b'<p><img src="a.png">t</p>',
    # <br/> 自闭合
    "br_self": b"<p>a<br/>b</p>",
    # 未闭合的 <b>：后面全部带粗体
    "unclosed_b": b"<p><b>bold forever",
    # 结束标签栈里没有的名字
    "stray_end": b"<p>a</span>b</p>",
    # 多层列表缩进级别
    "list": b"<ul><li>a<ul><li>b</li></ul></li></ul>",
    # <pre> 保留换行与连续空格
    "pre": b"<pre>a  b\n  c</pre>",
    # 数字实体边界：0 与超上限
    "ent_edge": b"<p>&#0;&#x10FFFF;&#xD800;&#65;</p>",
    # 实体不带分号
    "ent_nosemi": b"<p>a&amp b&#65 c</p>",
    # 注释里带 > 和引号
    "comment2": b"<p>a<!-- > \" ' -->b</p>",
    # 嵌套 opaque：svg 里再套 svg
    "svg": b"<p>a<svg><svg>x</svg></svg>b</p>",
    # <textarea> 当作 opaque 跳过
    "textarea": b"<p>a<textarea><p>hidden</p></textarea>b</p>",
    # title 里也有标签和实体
    "title2": b"<title>a<b>c</b>&amp;d</title><p>x</p>",
    # 超长属性名不匹配
    "longattr": b'<p class="aaaaaaaaaaaaaaaaaaaa">t</p>',
    # 尖括号裸奔
    "bare_lt": b"<p>1 < 2 > 0</p>",
    # 只有结束标签没有开标签
    "only_end": b"</div>text",
    # 空 href
    "empty_href": b'<a href="">t</a>',
}


def _as_html(entry):
    """CASES 里是 (html, expect)，EXTRA_PARSE 里是裸 html。统一取出来。"""
    return entry[0] if isinstance(entry, tuple) else entry


def _fmt_nodes(nodes):
    """把节点流格式化成可比对的形式（kind 0 用探针给的正文，其余用标签）"""
    out = []
    for nd in nodes:
        if nd.kind == R.NODE_TEXT and nd.text_len:
            out.append((nd.body, nd.style))
        else:
            out.append(("<%d/%d/%d>" % (nd.kind, nd.tag, nd.level),
                        nd.style))
    return out


def _py_nodes(r):
    """把 Python 侧节点的正文填进 body（探针那边是 parse 时直接填的）"""
    for nd in r.nodes:
        nd.body = r.text[nd.text_off:nd.text_off + nd.text_len]
    return _fmt_nodes(r.nodes)


def main():
    verbose = "-v" in sys.argv
    rustc = _find_rustc()
    if not rustc:
        print("SKIP: 找不到 rustc，宿主探针编不了（生产代码未受影响）")
        return 0
    if not build_probe(rustc):
        print("FAIL: 探针编译失败")
        return 1

    cases = dict(R.CASES)
    cases.update(EXTRA_PARSE)
    fails = []
    checked = 0

    # ---- 解析对拍 ----
    for name in sorted(cases):
        html = _as_html(cases[name])
        try:
            pnodes, pstats, ptitle = probe_parse(html)
        except Exception as e:                       # noqa: BLE001
            fails.append("%s: 探针异常 %s" % (name, e))
            continue
        py = R.parse(html)
        pytext = py.text
        got = _fmt_nodes(pnodes)
        want = _py_nodes(py)
        checked += 1
        if got != want:
            fails.append("parse/%s\n    rust=%r\n    py  =%r"
                         % (name, got, want))
            continue
        # 正文长度与链接数也要一致（buf 容量、溢出行为）
        if pstats[1] != len(pytext):
            fails.append("parse/%s 正文长度 rust=%d py=%d"
                         % (name, pstats[1], len(pytext)))
        if pstats[3] != py.truncated:
            fails.append("parse/%s truncated rust=%d py=%d"
                         % (name, pstats[3], py.truncated))
        # ref_html 自己也跑一遍它自己的期望值（防止"两边一致但一起错"）
        expect = cases[name][1]
        if name in R.CASES:
            mine = [(pytext[nd.text_off:nd.text_off + nd.text_len], nd.style)
                    for nd in py.nodes
                    if nd.kind == R.NODE_TEXT and nd.text_len]
            if mine != expect:
                fails.append("parse/%s 与手写期望不符 rust/py 一致但期望错\n"
                             "    expect=%r\n    got   =%r"
                             % (name, expect, mine))
        if verbose:
            print("  parse/%-12s ok" % name)

    # ---- 断行对拍 ----
    for data, cols, _expect in R.WRAP_CASES:
        checked += 1
        try:
            got = probe_wrap(data, cols)
        except Exception as e:                       # noqa: BLE001
            fails.append("wrap/%r/%d: 探针异常 %s" % (data[:16], cols, e))
            continue
        # 行末空格两边都不算内容
        got_s = [g.rstrip(b" \t\r\n") for g in got]
        want_s = [g.rstrip(b" \t\r\n")
                  for g in _py_wrap_lines(data, cols)]
        if got_s != want_s:
            fails.append("wrap/%r/%d\n    rust=%r\n    py  =%r"
                         % (data[:16], cols, got_s, want_s))
        elif verbose:
            print("  wrap/%-18r ok" % (data[:16],))

    # ---- 显示宽度 ----
    for data in (b"", b"abc", "你好世界".encode("utf-8"),
                 "a中b".encode("utf-8"), b"\xff\xfe", b"\xc3"):
        checked += 1
        got = probe_width(data)
        want = R.utf8_width(data)
        if got != want:
            fails.append("width/%r rust=%d py=%d" % (data, got, want))

    # ---- 生产侧的铁律：最终内核里绝不能出现边界检查符号 ----
    # panic_bounds_check 会把 core 的 fmt/浮点/net/unicode + 软浮点整条链
    # 拖进内核（实测 512KB -> 824KB）。
    #
    # **查 kernel_raw.bin（链接产物），不是 libezos_rs.a**：.a 里 core 的
    # CGU 本来就带 9 处 `U panic_bounds_check`（未定义引用），那是 core 自带的、
    # 链接器不会拉进来的。盯着 .a 数会得到"永远 9 处"的假警报，真回归了
    # 反而被当成老噪声忽略。真正要盯的是最终镜像里到底有没有被拖进来。
    #
    # 仓库不产 .elf（直接 objcopy 成 kernel_raw.bin），所以这里扫二进制。
    # kernel_raw.bin 是 objcopy 出来的裸镜像，nm 读不出符号名；
    # 但只要符号真被链进来，mangled 名会以明文留在字符串/符号表里，
    # 直接扫字节最可靠，也不依赖任何外部工具。
    checked += 1
    raw = os.path.join(ROOT, "kernel_raw.bin")
    if os.path.isfile(raw):
        with open(raw, "rb") as f:
            blob = f.read()
        if b"panic_bounds_check" in blob:
            fails.append("kernel_raw.bin 里出现 panic_bounds_check"
                         " —— html.rs 里多半有 slice 索引"
                         "（会拖进浮点/net，实测 +312KB）")
        elif verbose:
            print("  kernel_raw.bin 无 panic_bounds_check")
    # 还没构建就跳过这一项：体积闸门不该让解析对拍跟着红

    # ---- 内建样本与参考实现不许漂 ----
    checked += 1
    sfails = _sync_samples()
    fails.extend(sfails)
    if not sfails and verbose:
        print("  内建样本与 ref_html 一致")

    print("\n对拍 %d 项" % checked)
    if fails:
        print("%d FAILURES" % len(fails))
        for f in fails:
            print("FAIL  " + f)
        return 1
    print("ALL OK")
    return 0


def _py_wrap_lines(data, cols):
    starts = R.utf8_wrap(data, cols, 100)
    out = []
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(data)
        out.append(data[s:e])
    return out


if __name__ == "__main__":
    raise SystemExit(main())