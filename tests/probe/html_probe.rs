// html_probe.rs - **宿主侧探针**：直接编译生产用的那份
// `rust/ezos_rs/src/html.rs`，把它当库调，给 tests/ref_html.py 的独立
// Python 实现当对拍的另一端。
//
// 为什么要有这个：ref_html.py 是"人写的第二份实现"，两边可能一起错，
// 也可能只有一边错。如果只看 ref_html.py 自己的断言，绿了也说明不了
// 生产代码对。所以这里不改一行 html.rs，直接把它链进来跑 ——
// **对拍的另一端必须是生产代码本身**，不是它的副本。
//
// 用法（stdin -> stdout，行式协议，见 tests/test_html_probe.py）：
//   parse <html 的 hex>       逐行输出 "kind tag style level len hrefoff texthex"
//   wrap <cols> <text 的 hex> 逐行输出该行文本的 hex
//   width <text 的 hex>       输出一个十进制数字（字符数）
//
// 只在宿主上跑，与内核构建无关；内核侧走的是同一个 html.rs。
#![allow(dead_code)]

use std::io::Read;

// 生产实现，一个字都不改
#[path = "../../rust/ezos_rs/src/html.rs"]
mod html;

use html::{html_parse_rs, utf8_width_rs, utf8_wrap_rs, HtmlNode};

// 与 webview.c 的容量对齐（不必完全一样，只要能触发同样的边界分支；
// 故意用比内核小的 text_cap，好让 truncated 分支也被覆盖到）。
const MAX_NODES: usize = 3000;
const TEXT_CAP: usize = 40 * 1024;
const HREF_CAP: usize = 16 * 1024;
const ST_CAP: usize = 2048;

fn unhex(s: &str) -> Vec<u8> {
    let t: Vec<u8> = s
        .bytes()
        .filter(|c| !c.is_ascii_whitespace())
        .collect();
    let mut out = Vec::with_capacity(t.len() / 2);
    let mut i = 0;
    while i + 1 < t.len() {
        let hi = (t[i] as char).to_digit(16).unwrap_or(0) as u8;
        let lo = (t[i + 1] as char).to_digit(16).unwrap_or(0) as u8;
        out.push((hi << 4) | lo);
        i += 2;
    }
    out
}

fn hex(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for c in b {
        s.push_str(&format!("{:02x}", c));
    }
    s
}

// Vec 的空切片拿不到悬垂指针，这里显式给一个非空哑地址，
// 与 html_parse_rs 内部对 null 的处理保持一致。
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mode = args.get(1).map(|s| s.as_str()).unwrap_or("parse");
    let mut input = String::new();
    let _ = std::io::stdin().read_to_string(&mut input);

    match mode {
        "parse" => {
            let src = unhex(&input);
            // 用字节缓冲而不是 vec![HtmlNode{..}]：后者要求 Clone，
            // 而为了编探针去给生产结构体加 derive 是不该发生的改动。
            // HtmlNode 是 10 字节、无尾部 padding（webview.c 里有断言钉死）。
            assert_eq!(std::mem::size_of::<HtmlNode>(), 10);
            let mut node_raw = vec![0u8; MAX_NODES * 10];
            let nodes: *mut HtmlNode = node_raw.as_mut_ptr() as *mut HtmlNode;
            let mut text = vec![0u8; TEXT_CAP];
            let mut href = vec![0u8; HREF_CAP];
            let mut scratch = vec![0u8; ST_CAP];
            let mut stats = [0u32; 8];
            let n = unsafe {
                html_parse_rs(
                    src.as_ptr(),
                    src.len() as u32,
                    nodes,
                    MAX_NODES as u32,
                    text.as_mut_ptr(),
                    TEXT_CAP as u32,
                    href.as_mut_ptr(),
                    HREF_CAP as u32,
                    scratch.as_mut_ptr(),
                    ST_CAP as u32,
                    stats.as_mut_ptr(),
                )
            };
            println!("ret {}", n);
            println!("stats {} {} {} {} {} {}", stats[0], stats[1], stats[2],
                     stats[3], stats[4], stats[5]);
            let _tlen = stats[1] as usize;
            let hoff = stats[4] as usize;
            let hlen = stats[5] as usize;
            if hlen > 0 && hoff + hlen <= HREF_CAP {
                println!("title {}", hex(&href[hoff - 1..hoff - 1 + hlen]));
            } else {
                println!("title ");
            }
            let cnt = if n < 0 { 0 } else { n as usize };
            for k in 0..cnt {
                let nd = unsafe { &*nodes.add(k) };
                // 正文**不一定在 text 竞技场**：<img alt=...> 的 alt 存在
                // href 竞技场（那里还塞了 href 和 title），按 text_off 去
                // text[] 里取会越界。判据用 kind：IMG=4 走 href[]。
                let (buf, base): (&Vec<u8>, usize) = if nd.kind == 4 {
                    (&href, 0)
                } else {
                    (&text, 0)
                };
                let to = nd.text_off as usize;
                let tl = nd.text_len as usize;
                let body = if to + tl <= buf.len() - base.min(buf.len()) {
                    hex(&buf[base + to..base + to + tl])
                } else {
                    String::from("?")
                };
                println!(
                    "n {} {} {} {} {} {} {}",
                    nd.kind, nd.tag, nd.style, nd.level, nd.text_len,
                    nd.href_off, body
                );
            }
        }
        "wrap" => {
            let cols: u32 = args
                .get(2)
                .and_then(|s| s.parse().ok())
                .unwrap_or(80);
            let data = unhex(&input);
            let mut starts = vec![0u32; 4000];
            let cnt = unsafe {
                utf8_wrap_rs(
                    data.as_ptr(),
                    data.len() as u32,
                    cols,
                    starts.as_mut_ptr(),
                    starts.len() as u32,
                )
            };
            println!("ret {}", cnt);
            for k in 0..cnt as usize {
                let s = starts[k] as usize;
                let e = if k + 1 < cnt as usize {
                    starts[k + 1] as usize
                } else {
                    data.len()
                };
                if e > data.len() || s > e {
                    println!("line ?");
                } else {
                    println!("line {}", hex(&data[s..e]));
                }
            }
        }
        "width" => {
            let data = unhex(&input);
            let w = unsafe { utf8_width_rs(data.as_ptr(), data.len() as u32) };
            println!("{}", w);
        }
        other => {
            eprintln!("unknown mode {}", other);
            std::process::exit(2);
        }
    }
}