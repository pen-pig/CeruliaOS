/* http.c - HTTP/1.1 客户端实现。协议约定见 http.h 文件头。 */
#include "http.h"
#include "mem.h"
#include "net.h"

/* ---------- 小工具 ---------- */

static uint32_t h_strlen(const char *s) {
    uint32_t n = 0;
    while (s[n]) n++;
    return n;
}

/* 值拷贝：总是留终止符（超长则截断），返回是否被截断 */
static int h_copy_bounded(char *dst, uint32_t cap, const char *src, uint32_t n) {
    if (cap == 0) return 1;
    uint32_t w = (n < cap - 1) ? n : cap - 1;
    for (uint32_t i = 0; i < w; i++) dst[i] = src[i];
    dst[w] = '\0';
    return n > (cap - 1);
}

/* ASCII 小写。头字段名与 scheme / 状态行都要按大小写无关比较，
 * 踩过的坑：只把被比较的一侧转小写，另一侧保持原样 —— "Content-Length"
 * 永远匹配不上，报成"没有 Content-Length"或"状态行非法"。 */
static char h_lc(char c) {
    return (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
}

/* 大小写无关的前缀比较（scheme 与状态行本就不敏感） */
static int h_prefix_ci(const char *s, const char *pfx, uint32_t n) {
    for (uint32_t i = 0; i < n; i++)
        if (h_lc(s[i]) != h_lc(pfx[i])) return 0;
    return 1;
}

/* ---------- URL 解析 ---------- */

int http_parse_url(const char *url, char *host, uint32_t hostcap,
                   uint16_t *port, char *path, uint32_t pathcap,
                   uint8_t *is_https) {
    if (!url || !host || !port || !path || !is_https) return -1;
    uint32_t ulen = h_strlen(url);
    if (ulen == 0 || ulen >= 2048) return -1;      /* 长度异常 = fail closed */

    const char *p = url;
    uint16_t prt = 80;
    uint8_t https = 0;

    if (ulen >= 7 && h_prefix_ci(p, "http://", 7)) {
        p += 7;
    } else if (ulen >= 8 && h_prefix_ci(p, "https://", 8)) {
        p += 8; https = 1; prt = 443;
    } else {
        /* 裸 host：补默认路径 */
        if (h_copy_bounded(path, pathcap, "/", 1)) return -1;
        goto scheme_done;
    }

scheme_done:
    /* host 到 '/' 或 ':' 为止 */
    const char *h = p;
    uint32_t hn = 0;
    while (h[hn] && h[hn] != '/' && h[hn] != ':') hn++;
    if (hn == 0 || hn >= HTTP_HOST_MAX) return -1;

    if (h_copy_bounded(host, hostcap, h, hn)) return -1;   /* 主机名过长 */

    const char *q = h + hn;
    if (*q == ':') {
        q++;
        uint32_t pn = 0;
        while (q[pn] >= '0' && q[pn] <= '9') pn++;
        if (pn == 0 || pn > 5) return -1;                  /* 端口必须 1-5 位数字 */
        uint32_t v = 0;
        for (uint32_t i = 0; i < pn; i++) v = v * 10 + (uint32_t)(q[i] - '0');
        if (v == 0 || v > 0xFFFF) return -1;
        prt = (uint16_t)v;
        q += pn;
    }
    if (*q == '\0') {
        if (h_copy_bounded(path, pathcap, "/", 1)) return -1;
    } else {
        if (h_copy_bounded(path, pathcap, q, h_strlen(q))) return -1;
    }
    *port = prt;
    *is_https = https;
    return 0;
}

/* ---------- IPv4 字面量 ---------- */

/* 严格四段十进制，每段 0-255。拒绝 "1.2.3" / "010.1.1.1" 这类畸形写法：
 * 浏览器对前者的解释各不相同，统一按非法处理（fail closed）比猜一个稳。 */
static int parse_ipv4(const char *s, uint32_t *out_be) {
    uint32_t val = 0, parts = 0, oct = 0, digits = 0;
    for (uint32_t i = 0;; i++) {
        char c = s[i];
        if (c >= '0' && c <= '9') {
            if (++digits > 3) return -1;
            oct = oct * 10 + (uint32_t)(c - '0');
            if (oct > 255) return -1;
            continue;
        }
        if (c == '.' || c == '\0') {
            if (digits == 0) return -1;               /* 空段：".1.2.3" / "1..2.3" */
            val = (val << 8) | oct;
            parts++;
            oct = 0; digits = 0;
            if (c == '\0') break;
            if (parts == 4) return -1;                /* 第 4 段后还有 '.' */
            continue;
        }
        return -1;
    }
    if (parts != 4) return -1;
    *out_be = val;                                    /* 已是网络序 */
    return 0;
}

static int looks_like_ipv4(const char *s) {
    uint32_t i = 0;
    while (s[i] && s[i] != '/' && s[i] != ':') i++;
    if (i == 0) return 0;
    for (uint32_t k = 0; k < i; k++) if (s[k] != '.' && (s[k] < '0' || s[k] > '9'))
        return 0;
    return 1;
}

/* ---------- 行读取 ---------- */

#define H_RX_CHUNK   512
#define H_LINE_MAX   1024      /* 单行上限：超长即视为畸形（防无界增长） */

/* 大缓冲必须 static：内核栈只有 16KB/帧，check_stack.py 盯着。
 * fetch 是 shell 命令，单次调用，不会重入，故无需双缓冲。 */
static uint8_t  g_rxbuf[H_RX_CHUNK]  __attribute__((section(".bss")));
static char     g_line[H_LINE_MAX]   __attribute__((section(".bss")));
static char     g_req[HTTP_URL_MAX + 512] __attribute__((section(".bss")));

/* 从 socket 读一行（不含 CRLF，终止符 '\0'）。
 * 读到的字节已在 g_line 里。返回 >=0 行长，-1 = 连接断/超时，-2 = 行过长 */
static int http_read_line(int sid, uint32_t timeout_ms) {
    uint32_t n = 0;
    for (;;) {
        for (uint32_t i = 0; i < H_RX_CHUNK; i++) {
            int r = net_tcp_recv(sid, &g_rxbuf[i], 1, timeout_ms);
            if (r < 0) return -1;
            if (r == 0) return (n > 0) ? -1 : -1;    /* EOF：正常断线 */
            uint8_t c = g_rxbuf[i];
            if (c == '\n') {
                if (n > 0 && g_line[n - 1] == '\r') n--;
                g_line[n] = '\0';
                return (int)n;
            }
            if (n + 1 >= H_LINE_MAX) return -2;
            g_line[n++] = (char)c;
        }
    }
}

/* 从行里取 "Name: value" 的 value（跳过前导空格）。成功返回 0。 */
static int header_value(const char *line, const char *name, char *out, uint32_t cap) {
    uint32_t nn = h_strlen(name);
    for (uint32_t i = 0; i < nn; i++)
        if (h_lc(line[i]) != h_lc(name[i])) return -1;
    const char *v = line + nn;
    if (*v != ':') return -1;
    v++;
    while (*v == ' ' || *v == '\t') v++;               /* OWS */
    uint32_t vn = h_strlen(v);
    /* 行尾空白也该去掉（有些服务器会留） */
    while (vn > 0 && (v[vn - 1] == ' ' || v[vn - 1] == '\t')) vn--;
    if (h_copy_bounded(out, cap, v, vn)) return 1;    /* 1 = 截断 */
    return 0;
}

/* ---------- 响应体接收 ---------- */

/* 输出游标：out/pos/left 描述还能往哪写。满则丢弃多余并置 truncated，
 * 但**仍然继续把流读干净**——不读完就无法确认 chunk 边界/连接状态，
 * 半途丢包会让 socket 里留下垃圾，下次复用时错位。 */
typedef struct {
    uint8_t *buf;
    uint32_t cap;
    uint32_t pos;
    uint8_t  truncated;
} sink_t;

static void sink_put(sink_t *k, const uint8_t *d, uint32_t n) {
    for (uint32_t i = 0; i < n; i++) {
        if (k->pos < k->cap) k->buf[k->pos++] = d[i];
        else                 k->truncated = 1;
    }
}

/* 按 Content-Length 读定长响应体 */
static int read_by_length(int sid, sink_t *k, uint32_t want, uint32_t timeout_ms) {
    uint32_t left = want;
    uint8_t tmp[H_RX_CHUNK];
    while (left > 0) {
        uint32_t n = (left < H_RX_CHUNK) ? left : H_RX_CHUNK;
        int r = net_tcp_recv(sid, tmp, n, timeout_ms);
        if (r < 0) return -1;
        if (r == 0) return -2;            /* 比 Content-Length 短 = 对端截断 */
        sink_put(k, tmp, (uint32_t)r);
        left -= (uint32_t)r;
    }
    return 0;
}

/* 按 chunked 逐块读。返回 0 正常结束，-1 读错，-2 块长非法。
 * 块长是十六进制，可能带 ";ext"，只取分号前部分。 */
static int read_chunked(int sid, sink_t *k, uint32_t timeout_ms) {
    for (;;) {
        int ln = http_read_line(sid, timeout_ms);
        if (ln < 0) return -1;
        if (ln == 0) return -2;                       /* 空的 chunk 头 = 畸形 */

        uint32_t sz = 0, digits = 0;
        for (uint32_t i = 0; i < (uint32_t)ln; i++) {
            char c = g_line[i];
            if (c == ';') break;                      /* 扩展，整体丢弃 */
            uint32_t d;
            if      (c >= '0' && c <= '9') d = (uint32_t)(c - '0');
            else if (c >= 'a' && c <= 'f') d = (uint32_t)(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') d = (uint32_t)(c - 'A' + 10);
            else return -2;
            if (++digits > 8) return -2;               /* 荒谬长度，拒绝 */
            sz = (sz << 4) | d;
        }
        if (digits == 0) return -2;

        if (sz == 0) {
            /* 末块后是 trailer，空行结束；吃掉它就收工 */
            for (;;) {
                int t = http_read_line(sid, timeout_ms);
                if (t < 0) return -1;
                if (t == 0) return 0;
            }
        }
        int rc = read_by_length(sid, k, sz, timeout_ms);
        if (rc != 0) return rc;
        /* 每块数据后跟一个 CRLF，必须吃掉，否则下次读到的就是它 */
        int crlf = http_read_line(sid, timeout_ms);
        if (crlf < 0) return -1;
        if (crlf != 0) return -2;                     /* 必须是空行 */
    }
}

/* 读到连接关闭（服务器靠 close 定界，HTTP/1.0 常见） */
static int read_to_eof(int sid, sink_t *k, uint32_t timeout_ms) {
    uint8_t tmp[H_RX_CHUNK];
    for (;;) {
        int r = net_tcp_recv(sid, tmp, H_RX_CHUNK, timeout_ms);
        if (r < 0) return -1;
        if (r == 0) return 0;
        sink_put(k, tmp, (uint32_t)r);
    }
}

/* ---------- 一次请求/响应往返 ---------- */

#define H_TMP_VAL  256
static char g_hval[H_TMP_VAL] __attribute__((section(".bss")));
static char g_host[HTTP_HOST_MAX]  __attribute__((section(".bss")));
static char g_path[HTTP_PATH_MAX]  __attribute__((section(".bss")));
static char g_url[HTTP_URL_MAX]    __attribute__((section(".bss")));
static char g_rurl[HTTP_URL_MAX]   __attribute__((section(".bss")));

/* 跑一轮：连上、发 GET、读头、收体。重定向时把 Location 写进 *loc。
 * 返回 0 正常（含各种 3xx）；其余为错误码。 */
static int http_round(const char *host, uint16_t port, const char *path,
                      void *out, uint32_t outcap, uint32_t timeout_ms,
                      http_result_t *r, char *loc, uint32_t loccap) {
    uint32_t ip_be = 0;
    if (looks_like_ipv4(host)) {
        if (parse_ipv4(host, &ip_be) != 0) return -1;
    } else {
        if (net_dns_resolve(host, &ip_be, timeout_ms) != 0) return -2;
    }

    int sid = net_tcp_connect(ip_be, port, timeout_ms);
    if (sid < 0) return -3;

    /* 请求行 + Host。Host 里带非默认端口，与 RFC 7230 一致 */
    uint32_t rn = 0;
    const char *pre = "GET ";
    const char *suf = " HTTP/1.1\r\nHost: ";
    for (const char *q = pre; *q; q++) g_req[rn++] = *q;
    for (const char *q = path; *q; q++) g_req[rn++] = *q;
    for (const char *q = suf; *q; q++) g_req[rn++] = *q;
    for (const char *q = host; *q; q++) g_req[rn++] = *q;
    if (port != 80) {
        g_req[rn++] = ':';
        char pb[8]; uint32_t pn = 0;
        uint16_t v = port;
        do { pb[pn++] = (char)('0' + (v % 10u)); v /= 10u; } while (v);
        while (pn) g_req[rn++] = pb[--pn];
    }
    g_req[rn++] = '\r'; g_req[rn++] = '\n';
    const char *tail = "\r\nConnection: close\r\n\r\n";
    for (const char *q = tail; *q; q++) g_req[rn++] = *q;

    if (net_tcp_send(sid, g_req, rn, timeout_ms) < 0) { net_tcp_close(sid); return -4; }

    /* ---- 状态行：HTTP/1.x SP 3DIGIT ---- */
    int ln = http_read_line(sid, timeout_ms);
    if (ln < 0) { net_tcp_close(sid); return -5; }
    if (!h_prefix_ci(g_line, "HTTP/1.", 7)) { net_tcp_close(sid); return -6; }
    /* prefix 已含"HTTP/1."（小数点在 [6]），[7] 是版本末位，只允许 0/1 */
    uint32_t i = 7;
    if (g_line[i] < '0' || g_line[i] > '1') { net_tcp_close(sid); return -6; }
    i++;
    if (g_line[i] != ' ') { net_tcp_close(sid); return -6; }
    i++;
    if (g_line[i] < '0' || g_line[i] > '9' ||
        g_line[i+1] < '0' || g_line[i+1] > '9' ||
        g_line[i+2] < '0' || g_line[i+2] > '9') { net_tcp_close(sid); return -6; }
    int code = (g_line[i]-'0')*100 + (g_line[i+1]-'0')*10 + (g_line[i+2]-'0');
    r->status = code;

    /* ---- 头字段 ---- */
    int32_t clen = -1;
    int chunked = 0;
    loc[0] = '\0';
    for (;;) {
        ln = http_read_line(sid, timeout_ms);
        if (ln < 0) { net_tcp_close(sid); return -5; }
        if (ln == 0) break;                            /* 空行 = 头结束 */
        if (header_value(g_line, "Content-Length", g_hval, H_TMP_VAL) == 0) {
            uint32_t v = 0;
            for (uint32_t k = 0; g_hval[k]; k++) {
                if (g_hval[k] < '0' || g_hval[k] > '9') { v = 0xFFFFFFFFu; break; }
                v = v * 10 + (uint32_t)(g_hval[k] - '0');
                if (v > 0x7FFFFFFFu) { v = 0xFFFFFFFFu; break; }
            }
            clen = (v == 0xFFFFFFFFu) ? -2 : (int32_t)v;
        } else if (header_value(g_line, "Transfer-Encoding", g_hval, H_TMP_VAL) == 0) {
            if (h_prefix_ci(g_hval, "chunked", 7)) chunked = 1;
        } else if (header_value(g_line, "Content-Type", g_hval, H_TMP_VAL) == 0) {
            uint32_t vn = h_strlen(g_hval);
            if (vn > sizeof(r->content_type) - 1) vn = sizeof(r->content_type) - 1;
            for (uint32_t k = 0; k < vn; k++) r->content_type[k] = g_hval[k];
            r->content_type[vn] = '\0';
        } else if (loc[0] == '\0' &&
                   header_value(g_line, "Location", g_hval, H_TMP_VAL) == 0) {
            h_copy_bounded(loc, loccap, g_hval, h_strlen(g_hval));
        }
    }
    if (clen == -2) { net_tcp_close(sid); return -6; }

    /* ---- 3xx 且有 Location：正文不管，直接交给上层跟跳转 ---- */
    if (code >= 300 && code < 400 && code != 304 && loc[0] != '\0') {
        net_tcp_close(sid);
        r->chunked = 0;
        r->content_length = 0;
        return 0;
    }

    /* ---- 响应体 ---- */
    sink_t sink;
    sink.buf = (uint8_t *)out;
    sink.cap = outcap;
    sink.pos = 0;
    sink.truncated = 0;

    int rc;
    if (chunked) {
        rc = read_chunked(sid, &sink, timeout_ms);
        r->chunked = 1;
        r->content_length = 0;                        /* chunked 无确定长度 */
    } else if (clen >= 0) {
        rc = read_by_length(sid, &sink, (uint32_t)clen, timeout_ms);
        r->content_length = (uint32_t)clen;
    } else {
        rc = read_to_eof(sid, &sink, timeout_ms);      /* 无长度：靠 close 定界 */
        r->content_length = 0;
    }
    net_tcp_close(sid);

    r->body_len   = sink.pos;
    r->truncated  = sink.truncated;
    return (rc == 0) ? 0 : -5;
}

/* 把 Location 解析成绝对 URL：相对路径拼到原 host:port 上。
 * 成功 0，失败 -1（调用方按 -8 算重定向失败）。 */
static int resolve_redirect(const char *loc, const char *base_host,
                             uint16_t base_port, char *out, uint32_t cap) {
    uint32_t ln = h_strlen(loc);
    if (ln == 0) return -1;
    /* //host/path 继承 scheme */
    if (ln >= 2 && loc[0] == '/' && loc[1] == '/')
        return h_copy_bounded(out, cap, "http:", 5) ? -1 : 0;   /* 前缀补齐即可 */
    if (loc[0] == '/') {
        /* 拼 http://host + loc */
        char tmp[HTTP_URL_MAX];
        uint32_t n = 0;
        const char *pre = "http://";
        for (const char *q = pre; *q; q++) tmp[n++] = *q;
        for (const char *q = base_host; *q; q++) tmp[n++] = *q;
        if (base_port != 80) {
            tmp[n++] = ':';
            uint16_t v = base_port; char pb[8]; uint32_t pn = 0;
            do { pb[pn++] = (char)('0' + (v % 10u)); v /= 10u; } while (v);
            while (pn) tmp[n++] = pb[--pn];
        }
        for (uint32_t i = 0; i < ln; i++) tmp[n++] = loc[i];
        tmp[n] = '\0';
        if (n >= cap) return -1;
        for (uint32_t i = 0; i <= n; i++) out[i] = tmp[i];
        return 0;
    }
    /* 含 "://" 或以 host:port 开头的绝对 URL：直接用 */
    return h_copy_bounded(out, cap, loc, ln) ? -1 : 0;
}

int http_fetch(const char *url, void *out, uint32_t outcap,
               uint32_t timeout_ms, http_result_t *r) {
    if (!url || !out || outcap == 0 || !r || timeout_ms == 0) return -1;
    for (uint32_t i = 0; i < sizeof(http_result_t); i++) ((uint8_t *)r)[i] = 0;
    if (h_copy_bounded(g_url, sizeof(g_url), url, h_strlen(url))) return -1;

    uint32_t hop = 0;
    for (;;) {
        uint16_t port = 0;
        uint8_t https = 0;
        if (http_parse_url(g_url, g_host, sizeof(g_host), &port,
                           g_path, sizeof(g_path), &https) != 0) return -1;
        if (https) return -7;                          /* 没有 TLS 就直说 */
        if (port == 0) return -1;

        char loc[HTTP_URL_MAX];
        int rc = http_round(g_host, port, g_path, out, outcap, timeout_ms, r,
                            loc, sizeof(loc));
        if (rc != 0) return rc;

        int redir = (r->status >= 300 && r->status < 400 && r->status != 304);
        if (!redir || loc[0] == '\0') return 0;

        /* 防环：跳数超上限即失败，不继续 */
        if (++hop > HTTP_MAX_REDIR) return -8;
        if (resolve_redirect(loc, g_host, port, g_rurl, sizeof(g_rurl)) != 0)
            return -8;
        r->redirects = hop;
        /* 下一轮用新 URL */
        for (uint32_t i = 0; i < sizeof(g_url); i++) {
            g_url[i] = g_rurl[i];
            if (!g_rurl[i]) break;
        }
    }
}