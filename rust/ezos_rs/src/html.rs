//! HTML 解析 + UTF-8 断行（生产实现，纯计算）
//!
//! 把 `http_fetch` 抓回来的字节变成"能画的东西"。整条链路的失败语义先钉死：
//!
//! * **竞技场放不下 -> 置 truncated 标志，绝不覆盖、绝不越界**。
//!   半截标签被当正文显示，比明确报"内容被截断"糟糕得多。
//! * **实体解码失败 -> 原样保留 `&` 和后面的字符**，不吞字节。
//! * **畸形/未闭合标签 -> 忽略或复位开标签栈**，不 panic、**不死循环**。
//!
//! 为什么放 Rust 而不是 C：这是纯计算（输入字节 -> 定长记录），不碰内核资源，
//! 正合 rust_bridge.h 的判据。验证靠 `htmltest` 命令 + tests/ref_html.py 的
//! 宿主侧独立实现对拍，而不是再手写一份同样难读的 C 解析器。
//!
//! **全程裸指针（`*p.add(i)`），一个 slice 索引都不用**：任何一次 `s[i]`
//! 都会生成 `panic_bounds_check` 调用分支，而那个符号会把 core 的 fmt /
//! 浮点 / net / unicode + compiler_builtins 软浮点整条链拖进来 —— 实测
//! 内核 512KB -> 824KB（+312KB）。字符串一律用 `(*const u8, u32)` 对表示，
//! 长度由调用方担保。

#![allow(dead_code)]

// ---------------- 节点种类 ----------------
pub const NODE_TEXT: u8 = 0; // 一段同样式的正文
pub const NODE_BLOCK: u8 = 1; // 块级分隔（p/div/li/h1..）
pub const NODE_BR: u8 = 2; // <br>
pub const NODE_HR: u8 = 3; // <hr>
pub const NODE_IMG: u8 = 4; // <img>（只取 alt 文本）

// ---------------- 样式位 ----------------
pub const S_BOLD: u8 = 0x01;
pub const S_ITALIC: u8 = 0x02;
pub const S_UNDER: u8 = 0x04;
pub const S_MONO: u8 = 0x08;
pub const S_PRE: u8 = 0x10;
pub const S_STRIKE: u8 = 0x20;

// ---------------- 标签 id ----------------
pub const T_OTHER: u8 = 0;
pub const T_P: u8 = 1;
pub const T_DIV: u8 = 2;
pub const T_BR: u8 = 3;
pub const T_H1: u8 = 4;
pub const T_H2: u8 = 5;
pub const T_H3: u8 = 6;
pub const T_H4: u8 = 7;
pub const T_H5: u8 = 8;
pub const T_H6: u8 = 9;
pub const T_UL: u8 = 10;
pub const T_OL: u8 = 11;
pub const T_LI: u8 = 12;
pub const T_DL: u8 = 13;
pub const T_DT: u8 = 14;
pub const T_DD: u8 = 15;
pub const T_TABLE: u8 = 16;
pub const T_TR: u8 = 17;
pub const T_TD: u8 = 18;
pub const T_TH: u8 = 19;
pub const T_BLOCKQUOTE: u8 = 20;
pub const T_PRE: u8 = 21;
pub const T_HR: u8 = 22;
pub const T_CENTER: u8 = 23;
pub const T_SCRIPT: u8 = 24;
pub const T_STYLE: u8 = 25;
pub const T_HEAD: u8 = 26;
pub const T_TITLE: u8 = 27;
pub const T_A: u8 = 28;
pub const T_B: u8 = 29;
pub const T_STRONG: u8 = 30;
pub const T_I: u8 = 31;
pub const T_EM: u8 = 32;
pub const T_U: u8 = 33;
pub const T_S: u8 = 34;
pub const T_SPAN: u8 = 35;
pub const T_CODE: u8 = 36;
pub const T_TT: u8 = 37;
pub const T_SMALL: u8 = 38;
pub const T_BIG: u8 = 39;
pub const T_FONT: u8 = 40;
pub const T_LABEL: u8 = 41;
pub const T_IMG: u8 = 42;
pub const T_INPUT: u8 = 43;
pub const T_BODY: u8 = 44;
pub const T_HTML: u8 = 45;
pub const T_SECTION: u8 = 46;
pub const T_ARTICLE: u8 = 47;
pub const T_HEADER: u8 = 48;
pub const T_FOOTER: u8 = 49;
pub const T_NAV: u8 = 50;
pub const T_MAIN: u8 = 51;
pub const T_ASIDE: u8 = 52;
pub const T_META: u8 = 53;
pub const T_LINK: u8 = 54;
pub const T_BASE: u8 = 55;
pub const T_FORM: u8 = 56;
pub const T_FIELDSET: u8 = 57;
pub const T_HGROUP: u8 = 58;
pub const T_FIGURE: u8 = 59;
pub const T_FIGCAPTION: u8 = 60;
pub const T_ADDR: u8 = 61;
pub const T_DEL: u8 = 62;
pub const T_INS: u8 = 63;
pub const T_Q: u8 = 64;
pub const T_ABBR: u8 = 65;
pub const T_CITE: u8 = 66;
pub const T_DFN: u8 = 67;
pub const T_KBD: u8 = 68;
pub const T_SAMP: u8 = 69;
pub const T_VAR: u8 = 70;
pub const T_SUB: u8 = 71;
pub const T_SUP: u8 = 72;
pub const T_MARK: u8 = 73;
pub const T_TIME: u8 = 74;
pub const T_SELECT: u8 = 75;
pub const T_TEXTAREA: u8 = 76;
pub const T_BUTTON: u8 = 77;
pub const T_OPTION: u8 = 78;
pub const T_CAPTION: u8 = 79;
pub const T_COLGROUP: u8 = 80;
pub const T_COL: u8 = 81;
pub const T_THEAD: u8 = 82;
pub const T_TBODY: u8 = 83;
pub const T_TFOOT: u8 = 84;
pub const T_DETAILS: u8 = 85;
pub const T_SUMMARY: u8 = 86;
pub const T_DIALOG: u8 = 87;
pub const T_MENU: u8 = 88;
pub const T_PICTURE: u8 = 89;
pub const T_VIDEO: u8 = 90;
pub const T_AUDIO: u8 = 91;
pub const T_SOURCE: u8 = 92;
pub const T_NOSCRIPT: u8 = 93;
pub const T_TEMPLATE: u8 = 94;
pub const T_MAP: u8 = 95;
pub const T_AREA: u8 = 96;
pub const T_OBJECT: u8 = 97;
pub const T_PARAM: u8 = 98;
pub const T_EMBED: u8 = 99;
pub const T_IFRAME: u8 = 100;
pub const T_CANVAS: u8 = 101;
pub const T_SVG: u8 = 102;

/// 解析输出的一条记录，12 字节。字段宽度刻意选窄：一屏网页要装上千条。
#[repr(C)]
pub struct HtmlNode {
    pub text_off: u16, // 正文竞技场偏移
    pub text_len: u16,
    pub href_off: u16, // 0 = 无链接；否则 = href 竞技场偏移 + 1
    pub kind: u8,
    pub tag: u8,
    pub style: u8,
    pub level: u8, // 标题级别 / 列表层级
}

// 标签表。名字全小写，比较前对输入逐字节小写化 —— HTML 标签名大小写不敏感。
// 扫描时先比长度与首字节，再逐字节比，把 100 来项的比较压到平均 1~2 次循环。
static TAGS: &[(&[u8], u8)] = &[
    (b"a", T_A),
    (b"abbr", T_ABBR),
    (b"address", T_ADDR),
    (b"area", T_AREA),
    (b"article", T_ARTICLE),
    (b"aside", T_ASIDE),
    (b"audio", T_AUDIO),
    (b"b", T_B),
    (b"base", T_BASE),
    (b"bdi", T_OTHER),
    (b"bdo", T_OTHER),
    (b"big", T_BIG),
    (b"blockquote", T_BLOCKQUOTE),
    (b"body", T_BODY),
    (b"br", T_BR),
    (b"button", T_BUTTON),
    (b"canvas", T_CANVAS),
    (b"caption", T_CAPTION),
    (b"center", T_CENTER),
    (b"cite", T_CITE),
    (b"code", T_CODE),
    (b"col", T_COL),
    (b"colgroup", T_COLGROUP),
    (b"dd", T_DD),
    (b"del", T_DEL),
    (b"details", T_DETAILS),
    (b"dfn", T_DFN),
    (b"dialog", T_DIALOG),
    (b"div", T_DIV),
    (b"dl", T_DL),
    (b"dt", T_DT),
    (b"em", T_EM),
    (b"embed", T_EMBED),
    (b"fieldset", T_FIELDSET),
    (b"figcaption", T_FIGCAPTION),
    (b"figure", T_FIGURE),
    (b"font", T_FONT),
    (b"footer", T_FOOTER),
    (b"form", T_FORM),
    (b"h1", T_H1),
    (b"h2", T_H2),
    (b"h3", T_H3),
    (b"h4", T_H4),
    (b"h5", T_H5),
    (b"h6", T_H6),
    (b"head", T_HEAD),
    (b"header", T_HEADER),
    (b"hgroup", T_HGROUP),
    (b"hr", T_HR),
    (b"html", T_HTML),
    (b"i", T_I),
    (b"iframe", T_IFRAME),
    (b"img", T_IMG),
    (b"input", T_INPUT),
    (b"ins", T_INS),
    (b"kbd", T_KBD),
    (b"label", T_LABEL),
    (b"li", T_LI),
    (b"link", T_LINK),
    (b"main", T_MAIN),
    (b"map", T_MAP),
    (b"mark", T_MARK),
    (b"menu", T_MENU),
    (b"meta", T_META),
    (b"nav", T_NAV),
    (b"noscript", T_NOSCRIPT),
    (b"object", T_OBJECT),
    (b"ol", T_OL),
    (b"optgroup", T_OTHER),
    (b"option", T_OPTION),
    (b"output", T_OTHER),
    (b"p", T_P),
    (b"param", T_PARAM),
    (b"picture", T_PICTURE),
    (b"pre", T_PRE),
    (b"progress", T_OTHER),
    (b"q", T_Q),
    (b"rp", T_OTHER),
    (b"rt", T_OTHER),
    (b"ruby", T_OTHER),
    (b"s", T_S),
    (b"samp", T_SAMP),
    (b"script", T_SCRIPT),
    (b"section", T_SECTION),
    (b"select", T_SELECT),
    (b"small", T_SMALL),
    (b"source", T_SOURCE),
    (b"span", T_SPAN),
    (b"strike", T_S),
    (b"strong", T_STRONG),
    (b"style", T_STYLE),
    (b"sub", T_SUB),
    (b"summary", T_SUMMARY),
    (b"sup", T_SUP),
    (b"svg", T_SVG),
    (b"table", T_TABLE),
    (b"tbody", T_TBODY),
    (b"td", T_TD),
    (b"template", T_TEMPLATE),
    (b"textarea", T_TEXTAREA),
    (b"tfoot", T_TFOOT),
    (b"th", T_TH),
    (b"thead", T_THEAD),
    (b"time", T_TIME),
    (b"title", T_TITLE),
    (b"tr", T_TR),
    (b"tt", T_TT),
    (b"u", T_U),
    (b"ul", T_UL),
    (b"var", T_VAR),
    (b"video", T_VIDEO),
];

#[inline]
pub fn lower(b: u8) -> u8 {
    if b >= b'A' && b <= b'Z' {
        b + 32
    } else {
        b
    }
}

#[inline]
pub fn is_space(b: u8) -> bool {
    b == b' ' || b == b'\t' || b == b'\n' || b == b'\r' || b == 0x0C
}

/// 标签名 -> id。`s[at..at+n]` 的合法性由调用方保证（只扫到空白或 '>'）。
pub fn tag_id(s: *const u8, n: u32) -> u8 {
    if n == 0 || n > 12 {
        return T_OTHER;
    }
    unsafe {
        let first = lower(*s);
        for entry in TAGS.iter() {
            let name = entry.0.as_ptr();
            let ln = entry.0.len();
            if ln != n as usize || lower(*name) != first {
                continue;
            }
            let mut same = true;
            let mut i = 0usize;
            while i < ln {
                if lower(*s.add(i)) != *name.add(i) {
                    same = false;
                    break;
                }
                i += 1;
            }
            if same {
                return entry.1;
            }
        }
    }
    T_OTHER
}

fn is_void(t: u8) -> bool {
    t == T_BR
        || t == T_HR
        || t == T_IMG
        || t == T_INPUT
        || t == T_META
        || t == T_LINK
        || t == T_BASE
        || t == T_AREA
        || t == T_EMBED
        || t == T_PARAM
        || t == T_SOURCE
        || t == T_COL
}

/// 内容整体跳过（浏览器里有对应渲染，EZOS 上没有）。
fn is_opaque(t: u8) -> bool {
    t == T_NOSCRIPT
        || t == T_TEMPLATE
        || t == T_IFRAME
        || t == T_SVG
        || t == T_CANVAS
        || t == T_OBJECT
        || t == T_AUDIO
        || t == T_VIDEO
        || t == T_MAP
        || t == T_PICTURE
}

fn is_block(t: u8) -> bool {
    matches!(
        t,
        T_P | T_DIV
            | T_H1 | T_H2 | T_H3 | T_H4 | T_H5 | T_H6
            | T_UL | T_OL | T_LI | T_DL | T_DT | T_DD
            | T_TABLE | T_TR | T_TD | T_TH | T_THEAD | T_TBODY | T_TFOOT
            | T_BLOCKQUOTE | T_PRE | T_HR | T_CENTER
            | T_SECTION | T_ARTICLE | T_HEADER | T_FOOTER | T_NAV | T_MAIN | T_ASIDE
            | T_FORM | T_FIELDSET | T_HGROUP | T_FIGURE | T_FIGCAPTION | T_ADDR
            | T_DETAILS | T_SUMMARY | T_DIALOG | T_MENU | T_CAPTION | T_COLGROUP
    )
}

// ---------------- 解析器状态 ----------------

/// 开标签栈项在 scratch 里的字节布局（5 字节/条）
const ST_BYTES: u32 = 5;
const O_TAG: u32 = 0;
const O_STYLE: u32 = 1;
const O_HREF_LO: u32 = 2;
const O_HREF_HI: u32 = 3;
const O_PRE: u32 = 4;

struct HP {
    src: *const u8,
    len: u32,
    i: u32,
    nodes: *mut HtmlNode,
    max_nodes: u32,
    ncount: u32,
    text: *mut u8,
    text_cap: u32,
    tlen: u32,
    href: *mut u8,
    href_cap: u32,
    hlen: u32,
    links: u32,
    truncated: u32,
    run_start: u32,
    style: u8,
    href_off: u16,
    pre: u32,
    sp: u32,
    st: *mut u8,
    st_cap: u32,
    list_depth: i32,
}

impl HP {
    #[inline]
    unsafe fn at(&self, i: u32) -> u8 {
        *self.src.add(i as usize)
    }

    unsafe fn put(&mut self, b: u8) {
        if self.tlen >= self.text_cap {
            self.truncated = 1;
            return;
        }
        *self.text.add(self.tlen as usize) = b;
        self.tlen += 1;
    }

    unsafe fn emit(&mut self, kind: u8, tag: u8, level: u8, off: u32, len: u32, href_off: u16) {
        if self.ncount >= self.max_nodes {
            self.truncated = 1;
            return;
        }
        let p = self.nodes.add(self.ncount as usize);
        (*p).kind = kind;
        (*p).tag = tag;
        (*p).style = self.style;
        (*p).level = level;
        (*p).text_off = off as u16;
        (*p).text_len = len as u16;
        (*p).href_off = href_off;
        self.ncount += 1;
    }

    /// 收束当前 run（run_start..tlen）为一条文本节点。
    unsafe fn flush(&mut self) {
        if self.tlen > self.run_start {
            let off = self.run_start;
            let l = self.tlen - self.run_start;
            self.emit(NODE_TEXT, T_OTHER, 0, off, l, self.href_off);
        }
        self.run_start = self.tlen;
    }

    unsafe fn push(&mut self, tag: u8) {
        if (self.sp + 1) * ST_BYTES > self.st_cap {
            self.truncated = 1; // 栈满：忽略这条开标签，不 panic
            return;
        }
        let p = self.st.add((self.sp * ST_BYTES) as usize);
        *p.add(O_TAG as usize) = tag;
        *p.add(O_STYLE as usize) = self.style;
        *p.add(O_HREF_LO as usize) = (self.href_off & 0xFF) as u8;
        *p.add(O_HREF_HI as usize) = (self.href_off >> 8) as u8;
        *p.add(O_PRE as usize) = self.pre as u8;
        self.sp += 1;
    }

    /// 遇到 `</x>`：回退到最近一个同名开标签，恢复样式/href/pre。
    unsafe fn pop_to(&mut self, tag: u8) {
        let mut k = self.sp;
        while k > 0 {
            k -= 1;
            let p = self.st.add((k * ST_BYTES) as usize);
            let t = *p.add(O_TAG as usize);
            let st = *p.add(O_STYLE as usize);
            let h = (*p.add(O_HREF_LO as usize) as u16) | ((*p.add(O_HREF_HI as usize) as u16) << 8);
            let pr = *p.add(O_PRE as usize) as u32;
            if t == tag {
                self.style = st;
                self.href_off = h;
                self.pre = pr;
                self.sp = k;
                return;
            }
        }
        // 没有匹配的开标签（畸形 HTML 的常态）：栈复位，但**已收集的正文保留**。
        self.sp = 0;
        self.style = 0;
        self.href_off = 0;
        self.pre = 0;
    }

    /// 往 href 竞技场追加一条 NUL 分隔的字符串，返回 offset+1（0 = 失败）
    unsafe fn href_add(&mut self, off: u32, len: u32) -> u16 {
        if len == 0 || len > 1024 {
            return 0;
        }
        let end = off + len;
        if end >= self.href_cap {
            self.truncated = 1;
            return 0;
        }
        *self.href.add(end as usize) = 0;
        self.hlen = end + 1;
        self.links += 1;
        (end + 1) as u16
    }
}

// ---------------- 实体解码 ----------------

/// 把码点按 UTF-8 追加到 out。失败返回 0（调用方保留原字符）。
///
/// 落在代理区 / 越界的码点一律拒绝：**绝不产出 WTF-8** —— 那种字节流到了
/// 渲染层会一路画成乱码，而且没法从屏幕上看出是哪儿错了。
unsafe fn put_cp(out: *mut u8, cap: u32, used: &mut u32, cp: u32) -> u32 {
    if cp == 0 || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF) {
        return 0;
    }
    let n: u32 = if cp < 0x80 {
        1
    } else if cp < 0x800 {
        2
    } else if cp < 0x10000 {
        3
    } else {
        4
    };
    if *used + n > cap {
        return 0;
    }
    let o = *used as usize;
    if n == 1 {
        *out.add(o) = cp as u8;
    } else if n == 2 {
        *out.add(o) = 0xC0 | ((cp >> 6) as u8 & 0x1F);
        *out.add(o + 1) = 0x80 | ((cp & 0x3F) as u8);
    } else if n == 3 {
        *out.add(o) = 0xE0 | ((cp >> 12) as u8 & 0x0F);
        *out.add(o + 1) = 0x80 | (((cp >> 6) & 0x3F) as u8);
        *out.add(o + 2) = 0x80 | ((cp & 0x3F) as u8);
    } else {
        *out.add(o) = 0xF0 | ((cp >> 18) as u8 & 0x07);
        *out.add(o + 1) = 0x80 | (((cp >> 12) & 0x3F) as u8);
        *out.add(o + 2) = 0x80 | (((cp >> 6) & 0x3F) as u8);
        *out.add(o + 3) = 0x80 | ((cp & 0x3F) as u8);
    }
    *used += n;
    n
}

/// 命名实体表。刻意只收正文高频项 —— 完整表几百项在 no_std 里既占体积
/// 又几乎用不到。不认识的按原样保留 `&...`（见文件头契约）。
static ENTS: &[(&[u8], u32)] = &[
    (b"amp", 38), (b"lt", 60), (b"gt", 62), (b"quot", 34), (b"apos", 39),
    (b"nbsp", 32), (b"copy", 169), (b"reg", 174), (b"trade", 8482),
    (b"hellip", 8230), (b"mdash", 8212), (b"ndash", 8211),
    (b"lsquo", 8216), (b"rsquo", 8217), (b"ldquo", 8220), (b"rdquo", 8221),
    (b"bull", 8226), (b"middot", 183), (b"laquo", 171), (b"raquo", 187),
    (b"iexcl", 161), (b"iquest", 191), (b"cent", 162), (b"pound", 163),
    (b"yen", 165), (b"curren", 164), (b"brvbar", 166), (b"sect", 167),
    (b"uml", 168), (b"ordf", 170), (b"not", 172), (b"shy", 173),
    (b"ordm", 186), (b"macr", 175), (b"acute", 180), (b"cedil", 184),
    (b"sup1", 185), (b"sup2", 178), (b"sup3", 179), (b"deg", 176),
    (b"plusmn", 177), (b"micro", 181), (b"para", 182), (b"times", 215),
    (b"divide", 247), (b"frac14", 188), (b"frac12", 189), (b"frac34", 190),
    (b"dagger", 8224), (b"permil", 8240), (b"prime", 8242), (b"Prime", 8243),
    (b"lsaquo", 8249), (b"rsaquo", 8250), (b"oline", 8254), (b"frasl", 8260),
    (b"euro", 8364), (b"ensp", 8194), (b"emsp", 8195), (b"thinsp", 8201),
    (b"zwnj", 8204), (b"zwj", 8205), (b"lrm", 8206), (b"rlm", 8207),
    (b"aacute", 225), (b"eacute", 233), (b"egrave", 232), (b"agrave", 224),
    (b"ccedil", 231), (b"uuml", 252), (b"ouml", 246), (b"auml", 228),
    (b"szlig", 223), (b"ntilde", 241), (b"otilde", 245), (b"aring", 229),
];

/// `&...;` 的总字节长度（>=3），0 = 不是实体。
unsafe fn entity_len(src: *const u8, len: u32, at: u32) -> u32 {
    if at >= len || *src.add(at as usize) != b'&' {
        return 0;
    }
    let mut semi = at + 1;
    while semi < len && semi - at <= 12 && *src.add(semi as usize) != b';' {
        semi += 1;
    }
    if semi >= len || *src.add(semi as usize) != b';' {
        return 0;
    }
    semi - at + 1
}

/// 展开 src[at] 处的实体写进 out，返回消费的源字节数（0 = 不认识）。
unsafe fn decode_entity(
    src: *const u8,
    len: u32,
    at: u32,
    out: *mut u8,
    cap: u32,
    used: &mut u32,
) -> u32 {
    let total = entity_len(src, len, at);
    if total == 0 {
        return 0;
    }
    let body = at + 1;
    let semi = at + total - 1; // 指向 ';'
    let cp: u32;
    if *src.add(body as usize) == b'#' {
        let mut k = body + 1;
        if k >= semi {
            return 0;
        }
        let c0 = *src.add(k as usize);
        let hex = c0 == b'x' || c0 == b'X';
        if hex {
            k += 1;
        }
        let mut v = 0u32;
        let mut any = false;
        while k < semi {
            let c = *src.add(k as usize);
            let d = if c >= b'0' && c <= b'9' {
                (c - b'0') as u32
            } else if hex && c >= b'a' && c <= b'f' {
                (c - b'a' + 10) as u32
            } else if hex && c >= b'A' && c <= b'F' {
                (c - b'A' + 10) as u32
            } else {
                break;
            };
            v = v.saturating_mul(if hex { 16 } else { 10 }).saturating_add(d);
            if v > 0x10FFFF {
                return 0;
            }
            any = true;
            k += 1;
        }
        if !any {
            return 0;
        }
        cp = v;
    } else {
        // 命名实体：先比长度与首字节，再逐字节比
        let nl = semi - body;
        let mut found = 0u32;
        if nl > 0 && nl <= 8 {
            let nb = src.add(body as usize);
            let first = lower(*nb);
            for e in ENTS.iter() {
                let name = e.0.as_ptr();
                if e.0.len() != nl as usize || lower(*name) != first {
                    continue;
                }
                let mut same = true;
                let mut i = 0u32;
                while i < nl {
                    // 规范上大小写敏感，但 &AMP; / &Nbsp; 之类在真实网页里确实
                    // 存在；大小写不敏感匹配省得正文突然多出个字面量 "&AMP;"。
                    let c = *nb.add(i as usize);
                    let w = *name.add(i as usize);
                    if c != w && lower(c) != w {
                        same = false;
                        break;
                    }
                    i += 1;
                }
                if same {
                    found = e.1;
                    break;
                }
            }
        }
        if found == 0 {
            return 0;
        }
        cp = found;
    }
    if put_cp(out, cap, used, cp) == 0 {
        return 0;
    }
    total
}

// ---------------- 正文扫描 ----------------

unsafe fn scan_text(h: &mut HP) {
    while h.i < h.len {
        let c = h.at(h.i);
        if c == b'<' {
            break;
        }
        if c == b'&' {
            let mut used = h.tlen;
            let n = decode_entity(h.src, h.len, h.i, h.text, h.text_cap, &mut used);
            if n > 0 {
                h.tlen = used;
                h.i += n;
                continue;
            }
            // 不认识的实体：'&' 当普通字符（契约：绝不吞字节）
        }
        if h.pre == 0 && is_space(c) {
            // 空白折叠：连续空白压成单空格，且不在行首留空格
            if h.tlen > h.run_start && *h.text.add((h.tlen - 1) as usize) != b' ' {
                if h.tlen < h.text_cap {
                    *h.text.add(h.tlen as usize) = b' ';
                    h.tlen += 1;
                } else {
                    h.truncated = 1;
                }
            }
            h.i += 1;
            continue;
        }
        if h.pre == 0 && c < 0x20 {
            h.i += 1; // 其余控制字符直接丢（\r 已并入 is_space）
            continue;
        }
        h.put(c);
        h.i += 1;
    }
}

/// 跳过 `<!-- -->` / `<! ...>` / `<? ...>`。调用时 h.i 指向 '<'。
unsafe fn skip_decl(h: &mut HP) {
    if h.i + 3 < h.len && h.at(h.i + 1) == b'!' && h.at(h.i + 2) == b'-' && h.at(h.i + 3) == b'-' {
        let mut j = h.i + 4;
        while j + 2 < h.len {
            if h.at(j) == b'-' && h.at(j + 1) == b'-' && h.at(j + 2) == b'>' {
                h.i = j + 3;
                return;
            }
            j += 1;
        }
        h.i = h.len;
        return;
    }
    let mut j = h.i + 1;
    while j < h.len {
        if h.at(j) == b'>' {
            h.i = j + 1;
            return;
        }
        j += 1;
    }
    h.i = h.len;
}

fn raw_tag_len(t: u8) -> u32 {
    match t {
        T_SCRIPT => 6,
        T_STYLE => 5,
        T_TITLE => 5,
        T_NOSCRIPT => 8,
        T_TEMPLATE => 8,
        _ => 4,
    }
}

/// 跳到配对结束标签之后（<script>/<style>/<title> 的原始文本区）。
unsafe fn skip_raw(h: &mut HP, tag: u8) {
    let name_len = raw_tag_len(tag);
    let mut j = h.i;
    while j + 1 < h.len {
        if h.at(j) == b'<' && h.at(j + 1) == b'/' {
            let s = j + 2;
            let mut e = s;
            while e < h.len && !is_space(h.at(e)) && h.at(e) != b'>' {
                e += 1;
            }
            if e - s == name_len && tag_id(h.src.add(s as usize), e - s) == tag {
                let mut k = e;
                while k < h.len && h.at(k) != b'>' {
                    k += 1;
                }
                h.i = if k < h.len { k + 1 } else { h.len };
                return;
            }
        }
        j += 1;
    }
    h.i = h.len;
}

/// 跳过 <svg>/<canvas>/... 直到配对的结束标签（带深度计数）。
unsafe fn skip_opaque(h: &mut HP, tag: u8) {
    let mut depth = 1i32;
    let mut q = h.i;
    while q < h.len && depth > 0 {
        if h.at(q) == b'<' && q + 1 < h.len {
            let is_end = h.at(q + 1) == b'/';
            let s = if is_end { q + 2 } else { q + 1 };
            let mut e = s;
            while e < h.len && !is_space(h.at(e)) && h.at(e) != b'>' && h.at(e) != b'/' {
                e += 1;
            }
            if e > s && tag_id(h.src.add(s as usize), e - s) == tag {
                let mut k = e;
                while k < h.len && h.at(k) != b'>' {
                    k += 1;
                }
                if k >= h.len {
                    break;
                }
                if is_end {
                    depth -= 1;
                } else {
                    depth += 1;
                }
                q = k + 1;
                continue;
            }
        }
        q += 1;
    }
    h.i = q;
}

/// 在属性区 [from, to) 里找 name 的值（实体已展开），追加到 out 的 *used 处。
/// 返回写入字节数（0 = 没有这个属性）。
///
/// **每一轮扫描都保证 j 前进**，否则遇到以 `=` 开头的畸形属性会死循环。
unsafe fn read_attr(
    src: *const u8,
    from: u32,
    to: u32,
    name: *const u8,
    nlen: u32,
    out: *mut u8,
    cap: u32,
    used: &mut u32,
) -> u32 {
    let mark = *used;
    let mut j = from;
    while j < to {
        let c = *src.add(j as usize);
        if is_space(c) || c == b'/' || c == b'=' {
            j += 1;
            continue;
        }
        let ns = j;
        while j < to && !is_space(*src.add(j as usize)) && *src.add(j as usize) != b'='
            && *src.add(j as usize) != b'>'
        {
            j += 1;
        }
        if j == ns {
            j += 1;
            continue;
        }
        let nl = j - ns;
        // 属性值：跳过 '=' 与空白
        let mut k = j;
        while k < to && is_space(*src.add(k as usize)) {
            k += 1;
        }
        if k < to && *src.add(k as usize) == b'=' {
            k += 1;
            while k < to && is_space(*src.add(k as usize)) {
                k += 1;
            }
            if k < to {
                let q = *src.add(k as usize);
                let (vs, ve) = if q == b'"' || q == b'\'' {
                    let mut v = k + 1;
                    while v < to && *src.add(v as usize) != q {
                        v += 1;
                    }
                    (k + 1, v)
                } else {
                    let mut v = k;
                    while v < to && !is_space(*src.add(v as usize)) && *src.add(v as usize) != b'>' {
                        v += 1;
                    }
                    (v, v)
                };
                let mut p = vs;
                while p < ve {
                    if *src.add(p as usize) == b'&' {
                        let n = decode_entity(src, ve, p, out, cap, used);
                        if n > 0 {
                            p += n;
                            continue;
                        }
                    }
                    if *used < cap {
                        *out.add(*used as usize) = *src.add(p as usize);
                        *used += 1;
                    }
                    p += 1;
                }
            }
        }
        let mut matched = nl == nlen;
        if matched {
            let mut i = 0u32;
            while i < nl {
                if lower(*src.add((ns + i) as usize)) != *name.add(i as usize) {
                    matched = false;
                    break;
                }
                i += 1;
            }
        }
        if matched {
            return *used - mark;
        }
        *used = mark; // 不是要找的属性，回退重写（避免上次尝试的残留）
    }
    *used = mark;
    0
}

/// <title> 的内容：扫到结束标签，把纯文本（去标签、实体解码、空白折叠）
/// 追加进 out 的 *used 处，返回写入字节数。
unsafe fn extract_title(
    src: *const u8,
    len: u32,
    mut at: u32,
    out: *mut u8,
    cap: u32,
    used: &mut u32,
) -> u32 {
    let mark = *used;
    while at < len {
        let c = *src.add(at as usize);
        if c == b'<' {
            if at + 1 < len && *src.add((at + 1) as usize) == b'/' {
                break; // 结束标签 -> 收工
            }
            while at < len && *src.add(at as usize) != b'>' {
                at += 1;
            }
            at += 1;
            continue;
        }
        if is_space(c) {
            // 折叠：行首不留空格，连续空格压成一个
            let mut p = at;
            while p < len && is_space(*src.add(p as usize)) {
                p += 1;
            }
            let prev_blank = *used == mark || *out.add((*used - 1) as usize) == b' ';
            if !prev_blank && *used < cap {
                *out.add(*used as usize) = b' ';
                *used += 1;
            }
            at = p;
            continue;
        }
        if c == b'&' {
            let n = decode_entity(src, len, at, out, cap, used);
            if n > 0 {
                at += n;
                continue;
            }
        }
        if *used < cap {
            *out.add(*used as usize) = c;
            *used += 1;
        }
        at += 1;
    }
    while *used > mark && *out.add((*used - 1) as usize) == b' ' {
        *used -= 1;
    }
    *used - mark
}

// ---------------- 主入口 ----------------

/**
解析 HTML -> 节点数组 + 正文竞技场 + href 竞技场。

* `nodes`   至少 max_nodes 个 HtmlNode（12 字节/条）
* `text`    正文竞技场（实体已解码、空白已折叠）
* `href`    字符串竞技场（NUL 分隔）：链接目标、`<img alt>`、页面标题都放这
* `scratch` 开标签栈，st_cap 字节，5 字节/条
* `stats`   `[0]`=节点数 `[1]`=正文字节 `[2]`=链接数 `[3]`=truncated 标志
            `[4]`=title 在 href 竞技场的偏移+1（0=没抓到）`[5]`=title 长度

返回节点数（>=0）。**任何截断都只置 truncated 标志而不返回错误** ——
半个页面仍然比报错有用，但调用方必须能看见那个标志。
*/
#[no_mangle]
pub unsafe extern "C" fn html_parse_rs(
    html: *const u8,
    len: u32,
    nodes: *mut HtmlNode,
    max_nodes: u32,
    text: *mut u8,
    text_cap: u32,
    href: *mut u8,
    href_cap: u32,
    scratch: *mut u8,
    st_cap: u32,
    stats: *mut u32,
) -> i32 {
    if nodes.is_null() || text.is_null() || href.is_null() || stats.is_null() {
        return -1;
    }
    let empty = core::ptr::NonNull::<u8>::dangling().as_ptr();
    let mut h = HP {
        src: if html.is_null() { empty } else { html },
        len: if html.is_null() { 0 } else { len },
        i: 0,
        nodes,
        max_nodes,
        ncount: 0,
        text,
        text_cap,
        tlen: 0,
        href,
        href_cap,
        hlen: 0,
        links: 0,
        truncated: 0,
        run_start: 0,
        style: 0,
        href_off: 0,
        pre: 0,
        sp: 0,
        st: scratch,
        st_cap,
        list_depth: 0,
    };
    let mut title_off: u16 = 0;
    let mut title_len: u32 = 0;

    while h.i < h.len {
        let c = h.at(h.i);
        if c != b'<' {
            scan_text(&mut h);
            continue;
        }
        if h.i + 1 < h.len && (h.at(h.i + 1) == b'!' || h.at(h.i + 1) == b'?') {
            skip_decl(&mut h);
            continue;
        }
        let is_end = h.i + 1 < h.len && h.at(h.i + 1) == b'/';
        let ns = if is_end { h.i + 2 } else { h.i + 1 };
        let mut j = ns;
        while j < h.len && !is_space(h.at(j)) && h.at(j) != b'>' && h.at(j) != b'/' {
            j += 1;
        }
        if j == ns {
            h.put(b'<'); // 孤立的 '<'：当普通字符
            h.i += 1;
            continue;
        }
        let t = tag_id(h.src.add(ns as usize), j - ns);

        if is_end {
            if t == T_SCRIPT || t == T_STYLE {
                skip_raw(&mut h, t);
                continue;
            }
            if (t == T_UL || t == T_OL) && h.list_depth > 0 {
                h.list_depth -= 1;
            }
            // **每个结束标签都要 flush**：它是样式/链接的边界。
            // 早先只对块级和空元素 flush，结果 <a>x</a> 的 "x" 跟后面的正文
            // 并成同一段 run，链接点击区横跨两段内容。
            h.flush();
            h.pop_to(t);
            let mut k = j;
            while k < h.len && h.at(k) != b'>' {
                k += 1;
            }
            h.i = if k < h.len { k + 1 } else { h.len };
            continue;
        }

        // 开标签：属性区是 [j, '>' 之间)
        let mut k = j;
        while k < h.len && h.at(k) != b'>' {
            k += 1;
        }
        let attr_from = j;
        let attr_to = k;
        h.i = if k < h.len { k + 1 } else { h.len };

        if t == T_SCRIPT || t == T_STYLE {
            skip_raw(&mut h, t);
            continue;
        }
        if t == T_HEAD {
            continue; // head 里的东西一律不显示
        }
        if t == T_TITLE {
            // <title> 单独抓出来当窗口标题，不混进正文。存进 href 竞技场
            // ——正文竞技场后面还会继续追加，先写进去的位置会被当正文读出来。
            let mut used = h.hlen;
            let n = extract_title(h.src, h.len, h.i, h.href, h.href_cap, &mut used);
            if n > 0 && used < h.href_cap {
                *h.href.add(used as usize) = 0;
                h.hlen = used + 1;
                title_len = n;
                title_off = (used + 1) as u16;
            }
            skip_raw(&mut h, t);
            continue;
        }
        if is_opaque(t) {
            skip_opaque(&mut h, t);
            continue;
        }

        if t == T_BR {
            h.flush();
            h.emit(NODE_BR, T_BR, 0, 0, 0, 0);
            continue;
        }
        if t == T_HR {
            h.flush();
            h.emit(NODE_HR, T_HR, 0, 0, 0, 0);
            continue;
        }
        if t == T_IMG {
            let mut used = h.hlen;
            let alt = b"alt\0";
            let n = read_attr(h.src, attr_from, attr_to, alt.as_ptr(), 3, h.href, h.href_cap, &mut used);
            if n > 0 && used < h.href_cap {
                *h.href.add(used as usize) = 0;
                h.hlen = used + 1;
                h.emit(NODE_IMG, T_IMG, 0, used - n, n, 0);
            }
            continue;
        }

        if t == T_A {
            // 顺序要紧：**先 flush（带旧 href）再 push（存旧 href）最后改**。
            // 反过来做的话 <a>x</a> 的 "x" 会挂到**下一个**链接上，
            // 而且 </a> 恢复出来的是"已经带着新 href"的值，链接永远清不掉。
            h.flush();
            let mut used = h.hlen;
            let kw = b"href\0";
            let n = read_attr(h.src, attr_from, attr_to, kw.as_ptr(), 4, h.href, h.href_cap, &mut used);
            let new_off = h.href_add(used - n, n);
            h.push(t);
            h.href_off = new_off;
            continue;
        }

        // 行内样式：先断 run 再改样式（否则一段文字会带着半截旧样式）
        let bit = match t {
            T_B | T_STRONG => S_BOLD,
            T_I | T_EM => S_ITALIC,
            T_U | T_INS => S_UNDER,
            T_S | T_DEL => S_STRIKE,
            T_CODE | T_TT | T_KBD | T_SAMP | T_VAR => S_MONO,
            _ => 0,
        };
        // 下面每一条都是同一个模式：**flush 用旧样式收束当前 run，push 记下
        // 旧样式，然后才改状态**。顺序反过来样式就清不掉（见上面 T_A 的注释）。
        if bit != 0 {
            h.flush();
            h.push(t);
            h.style |= bit;
            continue;
        }
        if t == T_PRE {
            h.flush();
            h.push(t);
            h.style |= S_PRE;
            h.pre = 1;
            continue;
        }
        if t == T_UL || t == T_OL {
            h.flush();
            h.push(t);
            h.list_depth += 1;
            continue;
        }
        if t == T_LI {
            h.flush();
            let lv = if h.list_depth > 3 { 3u8 } else { h.list_depth as u8 };
            h.emit(NODE_BLOCK, T_LI, lv, 0, 0, 0);
            h.push(t);
            continue;
        }
        if t >= T_H1 && t <= T_H6 {
            h.flush();
            h.emit(NODE_BLOCK, t, t - T_H1 + 1, 0, 0, 0);
            h.push(t);
            continue;
        }
        if is_block(t) {
            h.flush();
            let lv = if h.list_depth > 0 { h.list_depth as u8 } else { 0 };
            h.emit(NODE_BLOCK, t, lv, 0, 0, 0);
            h.push(t);
            continue;
        }
        h.push(t);
    }
    h.flush();

    *stats.add(0) = h.ncount;
    *stats.add(1) = h.tlen;
    *stats.add(2) = h.links;
    *stats.add(3) = h.truncated;
    *stats.add(4) = title_off as u32;
    *stats.add(5) = title_len;
    h.ncount as i32
}

// ===================================================================
// UTF-8 断行
//
// 8x8 点阵是等宽的，所以断行退化成"每行能放几个字符" —— 但**不能按字节
// 切**：真实网页正文是 UTF-8，切在多字节序列中间会得到半个码点，渲染成
// 乱码，而且这种错肉眼看不出来（整段都"有字"）。这是整条排版链路里最
// 容易写错的一处，所以放 Rust 里用对拍钉死。
// ===================================================================

/// UTF-8 序列的字节长度；非法首字节按 1 处理（当替换字符）。
#[inline]
pub fn utf8_seq_len(b: u8) -> u32 {
    if b < 0x80 {
        1
    } else if b & 0xE0 == 0xC0 {
        2
    } else if b & 0xF0 == 0xE0 {
        3
    } else if b & 0xF8 == 0xF0 {
        4
    } else {
        1
    }
}

/// 该位置一个"合法字符"占多少字节。不合法（长度不足 / 续字节缺失 / 过长
/// 编码）一律返回 1 —— 画成替换字符，而不是崩。
#[inline]
unsafe fn seq_ok(src: *const u8, len: u32, at: u32) -> u32 {
    let b = *src.add(at as usize);
    if b < 0x80 {
        return 1;
    }
    if b == 0xC0 || b == 0xC1 || b >= 0xF5 {
        return 1; // 过长编码 / 永远非法
    }
    let l = utf8_seq_len(b);
    if at + l > len {
        return 1;
    }
    let mut i = 1u32;
    while i < l {
        if *src.add((at + i) as usize) & 0xC0 != 0x80 {
            return 1;
        }
        i += 1;
    }
    l
}

/// 文本的显示宽度（字符数，不是字节数）。
#[no_mangle]
pub unsafe extern "C" fn utf8_width_rs(s: *const u8, n: u32) -> u32 {
    if s.is_null() {
        return 0;
    }
    let mut i = 0u32;
    let mut w = 0u32;
    while i < n {
        i += seq_ok(s, n, i);
        w += 1;
    }
    w
}

/**
把 text 切成若干行，每行的**起始字节偏移**写进 starts（starts[0] 恒为 0）。

* `max_chars` 每行最多几个显示字符（必须 > 0）
* 返回行数；容量不够时返回已写出的行数（调用方应视为"排不下"）

断行规则：优先在空格处断（空格本身归入上一行末尾，渲染时裁掉）；单词过长
时在字符边界硬切，**绝不切进 UTF-8 序列中间**；显式 `\n` 直接断行且不占列。
*/
#[no_mangle]
pub unsafe extern "C" fn utf8_wrap_rs(
    text: *const u8,
    n: u32,
    max_chars: u32,
    starts: *mut u32,
    max_out: u32,
) -> u32 {
    if starts.is_null() || max_out == 0 {
        return 0;
    }
    *starts = 0;
    if n == 0 || max_chars == 0 {
        return 0;
    }

    let mut lines = 1u32;
    let mut i = 0u32; // 当前行起始（一定是字符边界）

    while i < n {
        if lines >= max_out {
            return lines;
        }
        let mut j = i;
        let mut col = 0u32;
        let mut space: i64 = -1; // 行内最后一个空格的位置（>= i）
        let mut filled = i; // 已装进本行的字节末尾（字符边界）
        let mut hit_nl = false;

        while j < n {
            let b = *text.add(j as usize);
            if b == b'\n' {
                hit_nl = true;
                j += 1;
                break;
            }
            // 空格/制表：记断点并吃掉，**不占列**。
            // 顺序很重要：先判 `col >= max_chars` 再看是不是空格，会让
            // "刚好装满 + 一个空格"退回上一个空格断开 —— "hello world"
            // 正好 11 字符，在 11 宽的行里被拆成 "hello" / "world"。
            if b == b' ' || b == b'\t' {
                space = j as i64;
                j += 1;
                continue;
            }
            if col >= max_chars {
                break;
            }
            col += 1;
            j += seq_ok(text, n, j);
            filled = j;
        }

        if hit_nl {
            *starts.add(lines as usize) = j;
            lines += 1;
            i = j;
            continue;
        }
        if filled >= n {
            break; // 剩下的全装下了，最后一行
        }
        if filled == i {
            // 一个字符都装不进（max_chars 太小 / 起始就不可断）：硬吃一个
            // 字符保证推进，否则死循环。
            filled = i + seq_ok(text, n, i);
            if filled <= i {
                break;
            }
            *starts.add(lines as usize) = filled;
            lines += 1;
            i = filled;
            continue;
        }
        if space >= 0 && space as u32 > i {
            // 回退到最后一个空格：行末不留空格，下行也不带空格进来
            let s = space as u32;
            let mut ns = s;
            while ns < n {
                let c = *text.add(ns as usize);
                if c != b' ' && c != b'\t' {
                    break;
                }
                ns += 1;
            }
            *starts.add(lines as usize) = ns;
            lines += 1;
            i = ns;
            continue;
        }
        // 硬切：filled 一定在字符边界上
        *starts.add(lines as usize) = filled;
        lines += 1;
        i = filled;
    }
    lines
}
