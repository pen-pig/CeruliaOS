# -*- coding: utf-8 -*-
"""ref_html.py - HTML 解析的宿主侧独立参考实现（对拍基线）

为什么要它：内核的解析器在 Rust 里（rust/ezos_rs/src/html.rs），**自己解析
自己写的页面永远绿** —— 排版错、实体错、UTF-8 切错，屏幕上看起来都还是
"有字的网页"，很难自己发现。所以这里用 Python 从零写一份**不共享任何代码**
的实现，跑同一批 HTML，比对节点流与正文。

对拍的三条（对应最常见的三个错）：
  1. 实体解码：&amp; &#123; &#x7B; 必须变成同一个字符；不认识的 &foo; 必须
     原样保留（不能吞字节）。
  2. UTF-8 断行：绝不能从多字节序列中间切开。这是排版链路最难肉眼发现的
     错 —— 切错了整段仍然"有字"，只是几个字变乱码。
  3. 空白折叠：连续空白压成一个、行首不留、行尾不留。

只覆盖"渲染需要区分"的部分（与 Rust 侧 NODE_*/T_* 的语义一致）：
不追求完整 HTML5，只求**同一批输入下两边逐字节一致**。
"""
import re

# ---- 节点种类（与 Rust 的 NODE_* 一致） ----
NODE_TEXT, NODE_BLOCK, NODE_BR, NODE_HR, NODE_IMG = 0, 1, 2, 3, 4

# ---- 样式位（与 Rust 的 S_* 一致） ----
S_BOLD, S_ITALIC, S_UNDER, S_MONO, S_PRE, S_STRIKE = (
    0x01, 0x02, 0x04, 0x08, 0x10, 0x20)

# 标签 id：只区分渲染需要的那几个，其余 0（T_OTHER）
_TAG_IDS = {
    'p': 1, 'div': 2, 'br': 3, 'h1': 4, 'h2': 5, 'h3': 6, 'h4': 7,
    'h5': 8, 'h6': 9, 'ul': 10, 'ol': 11, 'li': 12, 'dl': 13, 'dt': 14,
    'dd': 15, 'table': 16, 'tr': 17, 'td': 18, 'th': 19,
    'blockquote': 20, 'pre': 21, 'hr': 22, 'center': 23, 'script': 24,
    'style': 25, 'head': 26, 'title': 27, 'a': 28, 'b': 29, 'strong': 30,
    'i': 31, 'em': 32, 'u': 33, 's': 34, 'span': 35, 'code': 36, 'tt': 37,
    'small': 38, 'big': 39, 'font': 40, 'label': 41, 'img': 42,
    'input': 43, 'body': 44, 'html': 45, 'section': 46, 'article': 47,
    'header': 48, 'footer': 49, 'nav': 50, 'main': 51, 'aside': 52,
    'meta': 53, 'link': 54, 'base': 55, 'form': 56, 'fieldset': 57,
    'hgroup': 58, 'figure': 59, 'figcaption': 60, 'address': 61,
    'del': 62, 'ins': 63, 'q': 64, 'abbr': 65, 'cite': 66, 'dfn': 67,
    'kbd': 68, 'samp': 69, 'var': 70, 'sub': 71, 'sup': 72, 'mark': 73,
    'time': 74, 'select': 75, 'textarea': 76, 'button': 77, 'option': 78,
    'caption': 79, 'colgroup': 80, 'col': 81, 'thead': 82, 'tbody': 83,
    'tfoot': 84, 'details': 85, 'summary': 86, 'dialog': 87, 'menu': 88,
    'picture': 89, 'video': 90, 'audio': 91, 'source': 92, 'noscript': 93,
    'template': 94, 'map': 95, 'area': 96, 'object': 97, 'param': 98,
    'embed': 99, 'iframe': 100, 'canvas': 101, 'svg': 102,
}
# strike 是 s 的别名（Rust 侧同样处理）
_TAG_IDS['strike'] = 34

_VOID = {'br', 'hr', 'img', 'input', 'meta', 'link', 'base', 'area',
         'embed', 'param', 'source', 'col'}

_OPAQUE = {'noscript', 'template', 'iframe', 'svg', 'canvas', 'object',
           'audio', 'video', 'map', 'picture'}

_BLOCK = {'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li',
          'dl', 'dt', 'dd', 'table', 'tr', 'td', 'th', 'thead', 'tbody',
          'tfoot', 'blockquote', 'pre', 'hr', 'center', 'section', 'article',
          'header', 'footer', 'nav', 'main', 'aside', 'form', 'fieldset',
          'hgroup', 'figure', 'figcaption', 'address', 'details', 'summary',
          'dialog', 'menu', 'caption', 'colgroup'}

# 命名实体表（与 Rust 侧 ENTITIES 一一对应）
#
# **&nbsp; 这里映射成 0x20 而不是 U+00A0**，是刻意的近似：真正的
# non-breaking space 是 C2 A0，而 gfx 只画 ASCII、非 ASCII 一律画豆腐块，
# 映成 U+00A0 会让满页 nbsp 全变成豆腐块。映成 0x20 后不参与空白折叠
# （折叠只看源字节，实体展开后追加的内容不再折叠），所以
# "A &nbsp; B" 仍是两格宽，与真浏览器的观感一致。
_ENTS = {
    'amp': 38, 'lt': 60, 'gt': 62, 'quot': 34, 'apos': 39, 'nbsp': 32,
    'copy': 169, 'reg': 174, 'trade': 8482, 'hellip': 8230, 'mdash': 8212,
    'ndash': 8211, 'lsquo': 8216, 'rsquo': 8217, 'ldquo': 8220,
    'rdquo': 8221, 'bull': 8226, 'middot': 183, 'laquo': 171, 'raquo': 187,
    'iexcl': 161, 'iquest': 191, 'cent': 162, 'pound': 163, 'yen': 165,
    'curren': 164, 'brvbar': 166, 'sect': 167, 'uml': 168, 'ordf': 170,
    'not': 172, 'shy': 173, 'ordm': 186, 'macr': 175, 'acute': 180,
    'cedil': 184, 'sup1': 185, 'sup2': 178, 'sup3': 179, 'deg': 176,
    'plusmn': 177, 'micro': 181, 'para': 182, 'times': 215, 'divide': 247,
    'frac14': 188, 'frac12': 189, 'frac34': 190, 'dagger': 8224,
    'permil': 8240, 'prime': 8242, 'Prime': 8243, 'lsaquo': 8249,
    'rsaquo': 8250, 'oline': 8254, 'frasl': 8260, 'euro': 8364,
    'ensp': 8194, 'emsp': 8195, 'thinsp': 8201, 'zwnj': 8204, 'zwj': 8205,
    'lrm': 8206, 'rlm': 8207, 'aacute': 225, 'eacute': 233, 'egrave': 232,
    'agrave': 224, 'ccedil': 231, 'uuml': 252, 'ouml': 246, 'auml': 228,
    'szlig': 223, 'ntilde': 241, 'otilde': 245, 'aring': 229,
}


def _tag_id(name):
    if isinstance(name, bytes):
        try:
            name = name.decode('ascii')
        except UnicodeDecodeError:
            return 0
    return _TAG_IDS.get(name.lower(), 0)


# 标签名的合法终止符（与 Rust 的 is_space || '>' || '/' 判据一致）
_TAG_END = (b' ', b'\t', b'\n', b'\r', b'\x0c', b'>', b'/')


class Node(object):
    """与 Rust HtmlNode 字段一一对应（顺序也一致，便于逐字段比）"""
    # body 是**只有宿主探针会填**的额外字段：Rust 侧节点里存的是正文竞技场
    # 偏移，而两边缓冲大小不同、偏移不可比，所以对拍时正文一律走探针
    # 直接吐出来的字节。
    __slots__ = ('text_off', 'text_len', 'href_off', 'kind', 'tag',
                 'style', 'level', 'body')

    def __init__(self, kind, tag, style, level, text_off, text_len, href_off):
        self.text_off = text_off
        self.text_len = text_len
        self.href_off = href_off
        self.kind = kind
        self.tag = tag
        self.style = style
        self.level = level
        self.body = b''

    def key(self):
        return (self.kind, self.tag, self.style, self.level,
                self.text_len, self.href_off)

    def __repr__(self):
        return "N(kind=%d tag=%d style=%d lvl=%d len=%d href=%d)" % (
            self.kind, self.tag, self.style, self.level,
            self.text_len, self.href_off)


class Result(object):
    def __init__(self):
        self.nodes = []
        self.text = b''
        self.strs = b''       # NUL 分隔
        self.truncated = 0
        self.title = b''
        self.links = 0

    def summary(self):
        """给测试看的稳定摘要：不含绝对偏移（缓冲大小不同会变）"""
        out = []
        for n in self.nodes:
            out.append("%d/%d/%d/%d/%d/%d" % (
                n.kind, n.tag, n.style, n.level, n.text_len, n.href_off))
        return ";".join(out)


def _put_cp(cp):
    """码点 -> UTF-8 字节；代理区/越界返回 None（拒绝）"""
    if cp == 0 or cp > 0x10FFFF or (0xD800 <= cp <= 0xDFFF):
        return None
    return chr(cp).encode('utf-8')


def decode_entity(src, at):
    """展开 src[at] 处的实体。返回 (消费的字节数, 展开后的 bytes)；
    不认识返回 (0, b'')。"""
    if at >= len(src) or src[at:at + 1] != b'&':
        return 0, b''
    semi = at + 1
    while semi < len(src) and semi - at <= 12 and src[semi:semi + 1] != b';':
        semi += 1
    if semi >= len(src) or src[semi:semi + 1] != b';':
        return 0, b''
    total = semi - at + 1
    body = src[at + 1:semi]
    if body[:1] == b'#':
        rest = body[1:]
        try:
            if rest[:1] in (b'x', b'X'):
                cp = int(rest[1:], 16)
            else:
                cp = int(rest, 10)
        except ValueError:
            return 0, b''
        if cp > 0x10FFFF:
            return 0, b''
        enc = _put_cp(cp)
        return (total, enc) if enc else (0, b'')
    # 命名实体：大小写不敏感（与 Rust 侧一致）
    try:
        name = body.decode('ascii').lower()
    except UnicodeDecodeError:
        return 0, b''
    if len(name) > 8 or name not in _ENTS:
        return 0, b''
    enc = _put_cp(_ENTS[name])
    return (total, enc) if enc else (0, b'')


def _seq_ok(src, at):
    """一个合法字符占多少字节；不合法返回 1"""
    b = src[at]
    if b < 0x80:
        return 1
    if b in (0xC0, 0xC1) or b >= 0xF5:
        return 1
    if b & 0xE0 == 0xC0:
        ln = 2
    elif b & 0xF0 == 0xE0:
        ln = 3
    elif b & 0xF8 == 0xF0:
        ln = 4
    else:
        return 1
    if at + ln > len(src):
        return 1
    for k in range(1, ln):
        if src[at + k] & 0xC0 != 0x80:
            return 1
    return ln


def utf8_width(b):
    """显示宽度（字符数）"""
    i, w = 0, 0
    while i < len(b):
        i += _seq_ok(b, i)
        w += 1
    return w


def utf8_wrap(data, max_chars, max_out):
    """断行：返回每行的起始字节偏移。绝不在 UTF-8 序列中间切开。

    空输入返回**零行**（不是一行空行）—— 与 Rust 的 utf8_wrap_rs 一致，
    渲染层照着数画行，返回一行空行会多画一条空白。
    """
    if not data or max_chars <= 0 or max_out <= 0:
        return []
    starts = [0]
    i = 0
    n = len(data)
    while i < n:
        if len(starts) >= max_out:
            break
        j = i
        col = 0
        space = -1
        filled = i
        hit_nl = False
        while j < n:
            b = data[j:j + 1]
            if b == b'\n':
                hit_nl = True
                j += 1
                break
            # 空格先处理、不占列（顺序反了会把 "hello world" 拆成两行）
            if b in (b' ', b'\t'):
                space = j
                j += 1
                continue
            if col >= max_chars:
                break
            col += 1
            j += _seq_ok(data, j)
            filled = j
        if hit_nl:
            starts.append(j)
            i = j
            continue
        if filled >= n:
            break
        if filled == i:
            filled = i + _seq_ok(data, i)
            if filled <= i:
                break
            starts.append(filled)
            i = filled
            continue
        if space > i:
            ns = space
            while ns < n and data[ns:ns + 1] in (b' ', b'\t'):
                ns += 1
            starts.append(ns)
            i = ns
            continue
        starts.append(filled)
        i = filled
    return starts


def parse(html, max_nodes=3000, text_cap=40 * 1024, str_cap=16 * 1024):
    """解析 HTML -> Result。语义与 Rust 侧 html_parse_rs 逐条对应。"""
    r = Result()
    src = html if isinstance(html, bytes) else html.encode('utf-8')
    n = len(src)
    text = bytearray()
    strs = bytearray()
    nodes = r.nodes
    truncated = 0
    stack = []            # (tag, style, href_off, pre)
    style = 0
    href_off = 0
    pre = 0
    run_start = 0
    list_depth = 0
    i = 0

    def emit(kind, tag, level, off, ln, hoff):
        nonlocal truncated
        if len(nodes) >= max_nodes:
            truncated = 1
            return
        nodes.append(Node(kind, tag, style, level, off, ln, hoff))

    def flush():
        nonlocal run_start
        if len(text) > run_start:
            emit(NODE_TEXT, 0, 0, run_start, len(text) - run_start, href_off)
        run_start = len(text)

    def href_add(data):
        nonlocal truncated
        if not data or len(data) > 1024:
            return 0
        if len(strs) + len(data) + 1 >= str_cap:
            truncated = 1
            return 0
        off = len(strs)
        strs.extend(data)
        strs.append(0)
        return off + 1

    def read_attr(from_i, to_i, name):
        """在属性区找 name 的值（实体已展开）。返回 bytes 或 None"""
        j = from_i
        while j < to_i:
            c = src[j:j + 1]
            if c in (b' ', b'\t', b'\n', b'\r', b'\x0c', b'/', b'='):
                j += 1
                continue
            ns = j
            while j < to_i and src[j:j + 1] not in (b' ', b'\t', b'\n',
                                                    b'\r', b'\x0c', b'=', b'>'):
                j += 1
            if j == ns:
                j += 1
                continue
            key = src[ns:j].lower()
            k = j
            while k < to_i and src[k:k + 1] in (b' ', b'\t', b'\n', b'\r',
                                                b'\x0c'):
                k += 1
            val = b''
            if k < to_i and src[k:k + 1] == b'=':
                k += 1
                while k < to_i and src[k:k + 1] in (b' ', b'\t', b'\n',
                                                    b'\r', b'\x0c'):
                    k += 1
                if k < to_i:
                    q = src[k:k + 1]
                    if q in (b'"', b"'"):
                        v = k + 1
                        while v < to_i and src[v:v + 1] != q:
                            v += 1
                        val = _expand_entities(src[k + 1:v])
                    else:
                        v = k
                        while v < to_i and src[v:v + 1] not in (
                                b' ', b'\t', b'\n', b'\r', b'\x0c', b'>'):
                            v += 1
                        val = _expand_entities(src[k:v])
            if key == name:
                return val
        return None

    while i < n:
        c = src[i:i + 1]
        if c != b'<':
            # ---- 扫正文 ----
            while i < n:
                c = src[i:i + 1]
                if c == b'<':
                    break
                if c == b'&':
                    used, enc = decode_entity(src, i)
                    if used:
                        if len(text) + len(enc) > text_cap:
                            truncated = 1
                        else:
                            text.extend(enc)
                        i += used
                        continue
                if not pre and c in (b' ', b'\t', b'\n', b'\r', b'\x0c'):
                    if len(text) > run_start and text[-1:] != b' ':
                        if len(text) >= text_cap:
                            truncated = 1
                        else:
                            text.append(0x20)
                    i += 1
                    continue
                if not pre and c[0] < 0x20:
                    i += 1
                    continue
                if len(text) >= text_cap:
                    truncated = 1
                else:
                    text.extend(c)
                i += 1
            continue

        if i + 1 < n and src[i + 1:i + 2] in (b'!', b'?'):
            if src[i + 1:i + 4] == b'!--':
                j = src.find(b'-->', i + 4)
                i = (j + 3) if j >= 0 else n
            else:
                j = src.find(b'>', i + 1)
                i = (j + 1) if j >= 0 else n
            continue

        is_end = i + 1 < n and src[i + 1:i + 2] == b'/'
        ns = i + 2 if is_end else i + 1
        j = ns
        while j < n and src[j:j + 1] not in (b' ', b'\t', b'\n', b'\r',
                                             b'\x0c', b'>', b'/'):
            j += 1
        if j == ns:
            if len(text) >= text_cap:
                truncated = 1
            else:
                text.extend(b'<')
            i += 1
            continue
        try:
            name = src[ns:j].decode('ascii').lower()
        except UnicodeDecodeError:
            name = ''
        t = _tag_id(name)

        if is_end:
            if name in ('script', 'style'):
                i = _skip_raw(src, i + 1, name)
                continue
            if name in ('ul', 'ol') and list_depth > 0:
                list_depth -= 1
            # 每个结束标签都是样式/链接边界，必须 flush
            flush()
            for k in range(len(stack) - 1, -1, -1):
                if stack[k][0] == name:
                    _, style, href_off, pre = stack[k]
                    del stack[k:]
                    break
            else:
                style, href_off, pre = 0, 0, 0
                del stack[:]
            j2 = src.find(b'>', j)
            i = (j2 + 1) if j2 >= 0 else n
            continue

        k = src.find(b'>', j)
        attr_from, attr_to = j, (k if k >= 0 else n)
        i = (k + 1) if k >= 0 else n

        if name in ('script', 'style'):
            i = _skip_raw(src, i, name)
            continue
        if name == 'head':
            continue
        if name == 'title':
            # 抓标题（存 str 竞技场，不进正文）
            end = src.find(b'</', i)
            if end < 0:
                end = n
            raw = src[i:end]
            # 去内嵌标签
            raw = re.sub(rb'<[^>]*>', b'', raw)
            t2 = _expand_entities(raw)
            t2 = re.sub(rb'\s+', b' ', t2).strip(b' ')
            if t2:
                if len(strs) + len(t2) + 1 < str_cap:
                    strs.extend(t2)
                    strs.append(0)
                    r.title = t2
            i = _skip_raw(src, i, name)
            continue
        if name in _OPAQUE:
            i = _skip_opaque(src, i, name)
            continue

        if name == 'br':
            flush()
            emit(NODE_BR, T_BR if False else 3, 0, 0, 0, 0)
            continue
        if name == 'hr':
            flush()
            emit(NODE_HR, 22, 0, 0, 0, 0)
            continue
        if name == 'img':
            alt = read_attr(attr_from, attr_to, b'alt')
            if alt:
                off = len(strs)
                if len(strs) + len(alt) + 1 < str_cap:
                    strs.extend(alt)
                    strs.append(0)
                    emit(NODE_IMG, 42, 0, off, len(alt), 0)
            continue
        if name == 'a':
            # 顺序：先 flush（带旧 href）再 push（存旧 href）最后改
            flush()
            href = read_attr(attr_from, attr_to, b'href')
            new_off = href_add(href) if href else 0
            stack.append((name, style, href_off, pre))
            href_off = new_off
            continue

        bit = 0
        if name in ('b', 'strong'):
            bit = S_BOLD
        elif name in ('i', 'em'):
            bit = S_ITALIC
        elif name in ('u', 'ins'):
            bit = S_UNDER
        elif name in ('s', 'del'):
            bit = S_STRIKE
        elif name in ('code', 'tt', 'kbd', 'samp', 'var'):
            bit = S_MONO
        if bit:
            flush()
            stack.append((name, style, href_off, pre))
            style |= bit
            continue
        if name == 'pre':
            flush()
            stack.append((name, style, href_off, pre))
            style |= S_PRE
            pre = 1
            continue
        if name in ('ul', 'ol'):
            flush()
            stack.append((name, style, href_off, pre))
            list_depth += 1
            continue
        if name == 'li':
            flush()
            lv = 3 if list_depth > 3 else list_depth
            emit(NODE_BLOCK, 12, lv, 0, 0, 0)
            stack.append((name, style, href_off, pre))
            continue
        if name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            flush()
            emit(NODE_BLOCK, t, t - 3, 0, 0, 0)
            stack.append((name, style, href_off, pre))
            continue
        if name in _BLOCK:
            flush()
            lv = list_depth if list_depth > 0 else 0
            emit(NODE_BLOCK, t, lv, 0, 0, 0)
            stack.append((name, style, href_off, pre))
            continue
        stack.append((name, style, href_off, pre))

    flush()
    r.text = bytes(text)
    r.strs = bytes(strs)
    r.truncated = truncated
    r.links = sum(1 for _ in strs.split(b'\0')[:-1]) if strs else 0
    return r


def _expand_entities(raw):
    """把 raw 里的实体全展开（用于属性值与 title）"""
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i:i + 1] == b'&':
            used, enc = decode_entity(raw, i)
            if used:
                out.extend(enc)
                i += used
                continue
        out.extend(raw[i:i + 1])
        i += 1
    return bytes(out)


def _skip_raw(src, at, name):
    """跳到 </name> 之后"""
    pat = b'</' + name.encode('ascii')
    j = src.lower().find(pat, at)
    if j < 0:
        return len(src)
    k = src.find(b'>', j)
    return (k + 1) if k >= 0 else len(src)


def _skip_opaque(src, at, name):
    """跳到配对结束标签之后（带深度）。

    **逐位置扫描，不要用 find() 去猜边界**。早先那版用
    `find('<svg')` / `find('</svg')` 比距离，结果 `<p>a<svg>x</svg>b</p>`
    只留下 "a" —— "b" 被连着 </svg> 一起吞了。判据其实很简单：
    从当前位置起，看这个 '<' 后面跟的**标签名**是不是目标标签，
    是就看它是开标签（depth+1）还是结束标签（depth-1）。
    """
    depth = 1
    q = at
    n = len(src)
    tid = _tag_id(name)
    while q < n and depth > 0:
        if src[q:q + 1] == b'<' and q + 1 < n:
            is_end = src[q + 1:q + 2] == b'/'
            s = q + 2 if is_end else q + 1
            e = s
            while e < n and src[e:e + 1] not in _TAG_END:
                e += 1
            if e > s and _tag_id(src[s:e]) == tid:
                k = src.find(b'>', e)
                if k < 0:
                    break
                depth += -1 if is_end else 1
                q = k + 1
                continue
        q += 1
    return q


# ===================================================================
# 测试用例：HTML 样本 + 断行边界
# ===================================================================
# 每条都刻意挑了"最容易被写错"的地方，不是"最正常"的页面。
CASES = {
    # 实体：命名/十进制/十六进制/不认识的都要原样保留
    #
    # 期望值必须是 **bytes**：写成 Python str 的话 '\xe2\x80\xa6' 会被
    # 当成 5 个 Unicode 码点编码成 10 字节的 'â\x80¦'，比出来的差异
    # 落在编码上而不是解析上，看半天看不出真正的问题。
    #
    # 'B' 后面有**两个**空格：源里的空格 + &nbsp; 展开来的那个。
    # 别顺手把它"修"成一个 —— 那是把期望值改成迎合实现。
    'entity': (b'<p>a&amp;b &lt;c&gt; &#65; &#x42; &nbsp;&hellip; '
               b'&unknown; &amp</p>',
               [(b'a&b <c> A B  \xe2\x80\xa6 &unknown; &amp', 0)]),
    # 空白折叠：连续空白、行首、行尾
    'space': (b'  <p>  hello \t\n  world  </p>  ',
              [(b'hello world ', 0)]),
    # 标题分级 + 段落分段
    'head': (b'<h1>Title</h1><p>One</p><h2>Sub</h2><p>Two</p>',
             [(b'Title', 0), (b'One', 0), (b'Sub', 0), (b'Two', 0)]),
    # 链接：href 记进 str 竞技场，正文带 link 标记
    #
    # 'click' 与 'and ' **各成一段**（不是一整段）：</a> 触发 flush，
    # 否则链接的点击区会横跨后面两段内容。
    # 第二段是 'and ' 而**不是** ' and '：源里 </a> 后面那个前导空格
    # 落在空白折叠的"段首不留"里。前导空格曾经期望错写成 ' and '，
    # 看着像 bug 其实是折叠的正确行为 —— 这类"看起来该对"的期望值
    # 最容易掩盖真 bug，改之前先拿探针跑一遍。
    'link': (b'<a href="/a.html">click</a> and <a href="b.html">more</a>',
             [(b'click', 0), (b'and ', 0), (b'more', 0)]),
    # script/style 内容不进正文
    'script': (b'<p>a</p><script>var x = "<p>fake</p>";</script><p>b</p>',
               [(b'a', 0), (b'b', 0)]),
    # 嵌套样式
    # 'and ' 的归属同 'link' 用例：</b> 之后的前导空格被折叠掉。
    'style': (b'<p><b>bold</b> and <i>ital</i> and <code>mono</code></p>',
              [(b'bold', S_BOLD), (b'and ', 0), (b'ital', S_ITALIC),
               (b'and ', 0), (b'mono', S_MONO)]),
    # <br> 强制换行
    'br': (b'<p>line1<br>line2</p>', [(b'line1', 0), (b'line2', 0)]),
    # 图片：只取 alt
    'img': (b'<p><img src="x.png" alt="An image"> tail</p>',
            [(b'tail', 0)]),
    # title 不混进正文
    'title': (b'<html><head><title>Page T</title></head>'
              b'<body><p>body text</p></body></html>',
              [(b'body text', 0)]),
    # UTF-8 中文
    'utf8': ('<p>你好世界</p>'.encode('utf-8'),
             [('你好世界'.encode('utf-8'), 0)]),
    # 畸形：未闭合标签（内容必须保留）
    'unclosed': (b'<div><p>text without closing', [(b'text without closing', 0)]),
    # 畸形：孤立的 <
    'lone_lt': (b'<p>a < b</p>', [(b'a < b', 0)]),
    # 空标签
    'empty': (b'<p></p><br><hr>', []),
    # 注释
    'comment': (b'<p>a<!-- <p>hidden</p> -->b</p>', [(b'ab', 0)]),
}


def check_cases():
    """跑一遍样本，返回 [(名字, ok, 详情)]"""
    out = []
    for name, (html, expect) in CASES.items():
        r = parse(html)
        got = [(r.text[n.text_off:n.text_off + n.text_len], n.style)
               for n in r.nodes if n.kind == NODE_TEXT and n.text_len]
        ok = got == expect
        out.append((name, ok, "" if ok else "want=%r got=%r" % (expect, got)))
    return out


# ---- 断行对拍向量 ----
# 三个最危险的：超长单词、多字节中文刚好卡在边界、显式换行
WRAP_CASES = [
    # (输入, 每行几个字符, 期望每行的文本)
    (b'hello world foo bar', 11, [b'hello world', b'foo bar']),
    # 刚好装满 + 后面紧跟空格：不能退回上一个空格拆行
    (b'abcdefghijk lmn', 11, [b'abcdefghijk', b'lmn']),
    # 一个字都放不下时的硬切（必须推进，不能死循环）
    (b'abcdef', 1, [b'a', b'b', b'c', b'd', b'e', b'f']),
    # 多字节刚好卡边界：8 字节的"你好世界"= 4 字符，每行放 3 个字符
    # -> 3 + 1。写成 2+1+1 是把"每行 3 字符"看成了"每行 3 **字节**"，
    # 恰好是这一段要防的错（按字节切会把字切坏）。
    ('你好世界'.encode('utf-8'), 3,
     ['你好世'.encode('utf-8'), '界'.encode('utf-8')]),
    # 显式换行
    (b'a\nb\nc', 10, [b'a', b'b', b'c']),
    # 行内没有空格可退时硬切（哪怕切在词中间）：'verylongwordindeed'
    # 有 18 个字符，10 宽的行装不下，只能切。空格优先，所以第一行是
    # 'short'（断在上一个空格），剩下才硬切。
    (b'short verylongwordindeed end', 10,
     [b'short', b'verylongwo', b'rdindeed', b'end']),
    # 硬切绝不能切进 UTF-8 序列中间：'中'在 2 字符宽的行里必须
    # 整字换行，哪怕看起来"浪费"一列。
    ('中文'.encode('utf-8'), 1, ['中'.encode('utf-8'), '文'.encode('utf-8')]),
    # 空串 / 零宽 / 零列：都不能死循环，且一律返回**零行**
    # （返回一行空行会让渲染层多画一条空白）
    (b'', 10, []),
    (b'abc', 0, []),
]


def check_wrap():
    out = []
    for data, mc, expect in WRAP_CASES:
        starts = utf8_wrap(data, mc, 100)
        got = []
        for k, s in enumerate(starts):
            e = starts[k + 1] if k + 1 < len(starts) else len(data)
            got.append(data[s:e])
        # 行末空格不参与比较（渲染时会裁）
        got = [g.rstrip(b' \t\r\n') for g in got]
        ok = got == expect
        out.append(("wrap:%r/%d" % (data[:16], mc), ok,
                    "" if ok else "want=%r got=%r" % (expect, got)))
    return out


if __name__ == '__main__':
    fails = 0
    for name, ok, detail in check_cases() + check_wrap():
        print("%-6s %s %s" % ("PASS" if ok else "FAIL", name, detail))
        if not ok:
            fails += 1
    print("\n%s" % ("ALL OK" if fails == 0 else "%d FAILURES" % fails))
    raise SystemExit(1 if fails else 0)
