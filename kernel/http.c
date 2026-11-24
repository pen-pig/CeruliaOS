/* http.c - HTTP/1.1 客户端实现。协议约定见 http.h 文件头。 */
#include "http.h"
#include "mem.h"
#include "net.h"
#include "isr.h"
#include "auth.h"
#include "rtc.h"
#include "websession.h"

int tls_start(int, const char *);
int tls_read(void *, unsigned);
int tls_write(const void *, unsigned);
void tls_forget(void);
static int secure, fetch_owned;
static const char *image_origin; /* immutable for the ownership interval */
static int cross_image;
static int (*image_cancelled)(void);
static int style_resource;
static uint32_t web_token,post_len;
static const uint8_t *post_body;
static int send_cookies;
static uint32_t deadline;
uint32_t http_remaining(void) {
    if(image_cancelled && image_cancelled())return 0;
    int32_t left = (int32_t)(deadline - g_pit_ticks);
    if(image_origin && left>3500)left=3500;
    return left > 0 ? (uint32_t)left : 0;
}

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
    if (ulen == 0 || ulen >= HTTP_URL_MAX) return -1;
    for (uint32_t i=0;i<ulen;i++)
        if ((uint8_t)url[i] < 32 || url[i] == 127 || url[i] == '\\') return -1;

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
    while (h[hn] && h[hn] != '/' && h[hn] != ':' && h[hn] != '?' && h[hn] != '#') hn++;
    if (hn == 0 || hn >= HTTP_HOST_MAX) return -1;
    for (uint32_t i=0;i<hn;i++) {
        char c=h_lc(h[i]);
        if (!((c>='a'&&c<='z')||(c>='0'&&c<='9')||c=='.'||c=='-')) return -1;
    }

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
    if (*q && *q != '/' && *q != '?' && *q != '#') return -1;
    uint32_t pn=0;
    if (pathcap<2) return -1;
    if (*q!='/') path[pn++]='/';
    /* The fragment stays client-side. Encode UTF-8 path/query bytes for HTTP. */
    static const char hex[]="0123456789ABCDEF";
    for (;*q && *q!='#';q++) {
        uint8_t c=(uint8_t)*q;
        if(c>=128 || c==' ') {
            if(pathcap-pn<=3) return -1;
            path[pn++]='%'; path[pn++]=hex[c>>4]; path[pn++]=hex[c&15];
        } else {
            if(pathcap-pn<=1) return -1;
            path[pn++]=(char)c;
        }
    }
    path[pn]=0;
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
#define H_LINE_MAX   4096      /* Room for real-world CSP and Location lines. */

/* 大缓冲必须 static：内核栈只有 16KB/帧，check_stack.py 盯着。
 * fetch 是 shell 命令，单次调用，不会重入，故无需双缓冲。 */
static uint8_t  g_rxbuf[H_RX_CHUNK]  __attribute__((section(".bss")));
static char     g_line[H_LINE_MAX]   __attribute__((section(".bss")));
#define WEB_COOKIE_SPACE 8192
static char     g_req[HTTP_URL_MAX + 512 + WEB_COOKIE_SPACE] __attribute__((section(".bss")));
static unsigned rx_pos, rx_end;

static int stream_read(int sid, void *dst, uint32_t n, uint32_t timeout) {
    uint32_t left=http_remaining();
    if (!left) return -1;
    if (left>timeout) left=timeout;
    if (rx_pos == rx_end) {
        int r=secure ? tls_read(g_rxbuf,sizeof(g_rxbuf)) :
            net_tcp_recv(sid,g_rxbuf,sizeof(g_rxbuf),left);
        if (r<=0) return r;
        rx_pos=0; rx_end=(unsigned)r;
    }
    if (n>rx_end-rx_pos) n=rx_end-rx_pos;
    memcpy(dst,g_rxbuf+rx_pos,n); rx_pos+=n;
    return (int)n;
}

/* 从 socket 读一行（不含 CRLF，终止符 '\0'）。
 * 读到的字节已在 g_line 里。返回 >=0 行长，-1 = 连接断/超时，-2 = 行过长 */
static int http_read_line(int sid, uint32_t timeout_ms) {
    uint32_t n = 0;
    for (;;) {
        for (uint32_t i = 0; i < H_RX_CHUNK; i++) {
            uint8_t c;
            int r = stream_read(sid, &c, 1, timeout_ms);
            if (r < 0) return -1;
            if (r == 0) return (n > 0) ? -1 : -1;    /* EOF：正常断线 */
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
    uint32_t room=k->cap-k->pos;
    if(n>room){n=room;k->truncated=1;}
    memcpy(k->buf+k->pos,d,n); k->pos+=n;
}

/* 按 Content-Length 读定长响应体 */
static int read_by_length(int sid, sink_t *k, uint32_t want, uint32_t timeout_ms) {
    uint32_t left = want;
    uint8_t tmp[H_RX_CHUNK];
    while (left > 0) {
        uint32_t n = (left < H_RX_CHUNK) ? left : H_RX_CHUNK;
        if (k->pos == k->cap) { k->truncated = 1; return 0; }
        int r = stream_read(sid, tmp, n, timeout_ms);
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
        if (k->truncated) return 0;
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
        int r = stream_read(sid, tmp, H_RX_CHUNK, timeout_ms);
        if (r < 0) return -1;
        if (r == 0) return 0;
        sink_put(k, tmp, (uint32_t)r);
        if (k->truncated) return 0;
    }
}

/* ---------- 一次请求/响应往返 ---------- */

#define H_TMP_VAL  HTTP_URL_MAX
static char g_hval[H_TMP_VAL] __attribute__((section(".bss")));
static char g_host[HTTP_HOST_MAX]  __attribute__((section(".bss")));
static char g_path[HTTP_PATH_MAX]  __attribute__((section(".bss")));
static char g_url[HTTP_URL_MAX]    __attribute__((section(".bss")));
static char g_rurl[HTTP_URL_MAX]   __attribute__((section(".bss")));

/* Short cooldown, not a boot-long latch: retry when a link comes up later. */
static uint8_t g_auto_dhcp_tried;
static uint32_t g_auto_dhcp_last;

/* 跑一轮：连上、发 GET、读头、收体。重定向时把 Location 写进 *loc。
 * 返回 0 正常（含各种 3xx）；其余为错误码。 */
static int http_round(const char *host, uint16_t port, const char *path,
                      void *out, uint32_t outcap, uint32_t timeout_ms,
                      http_result_t *r, char *loc, uint32_t loccap) {
    uint32_t ip_be = 0;
    if (looks_like_ipv4(host)) {
        if (parse_ipv4(host, &ip_be) != 0) return -1;
    } else {
        /* 没有租约就没有 DNS 服务器，浏览器只会报 "DNS lookup failed"——
         * 域名访问前自动拿一次租约（user-net / 局域网开箱即用）。IP 直连
         * 不走这里，行为不变。 */
        if (!net_dhcp_valid() && netdev_present() &&
            (!g_auto_dhcp_tried || g_pit_ticks - g_auto_dhcp_last >= 5000u)) {
            uint32_t budget = http_remaining() / 8u;
            if (budget > 1000u) budget = 1000u;
            if (budget) net_dhcp(budget);
            g_auto_dhcp_last = g_pit_ticks;
            g_auto_dhcp_tried = 1;
        }
        if (net_dns_resolve(host, &ip_be, timeout_ms) != 0) return -2;
    }

    if(cross_image){
        uint32_t a=ip_be>>24, b=(ip_be>>16)&255;
        if(!a || a==10 || a==127 || a>=224 || (a==169 && b==254) ||
           (a==172 && b>=16 && b<=31) || (a==192 && (b==0 || b==168)) ||
           (a==100 && b>=64 && b<=127) || (a==198 && (b==18 || b==19)))return -14;
    }
    int sid = net_tcp_connect(ip_be, port, timeout_ms);
    if (sid < 0) return -3;
    rx_pos=rx_end=0;
    if (secure) {
        int err=tls_start(sid,host);
        if (err) { net_tcp_close(sid); return err; }
    }

    /* 请求行 + Host。Host 里带非默认端口，与 RFC 7230 一致 */
    uint32_t rn = 0;
    const char *pre = post_len?"POST ":"GET ";
    const char *suf = " HTTP/1.1\r\nHost: ";
    for (const char *q = pre; *q; q++) g_req[rn++] = *q;
    for (const char *q = path; *q; q++) g_req[rn++] = *q;
    for (const char *q = suf; *q; q++) g_req[rn++] = *q;
    for (const char *q = host; *q; q++) g_req[rn++] = *q;
    unsigned authority_start=rn-h_strlen(host);
    if (port != (secure ? 443 : 80)) {
        g_req[rn++] = ':';
        char pb[8]; uint32_t pn = 0;
        uint16_t v = port;
        do { pb[pn++] = (char)('0' + (v % 10u)); v /= 10u; } while (v);
        while (pn) g_req[rn++] = pb[--pn];
    }
    unsigned authority_len=rn-authority_start;
    g_req[rn++] = '\r'; g_req[rn++] = '\n';
    if(secure&&web_token&&send_cookies){
        uint32_t now=0;
        if(!rtc_unix(&now)){
            int n=web_session_header(web_token,host,port,path,g_req+rn+8,WEB_COOKIE_SPACE-12,now);
            if(n<0){net_tcp_close(sid);return -18;}
            if(n){memcpy(g_req+rn,"Cookie: ",8);rn+=8+(unsigned)n;g_req[rn++]='\r';g_req[rn++]='\n';}
        }
    }
    if(post_len){
        const char *fields="Origin: https://";
        for(const char *q=fields;*q;q++)g_req[rn++]=*q;
        memcpy(g_req+rn,g_req+authority_start,authority_len);rn+=authority_len;
        fields="\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: ";
        for(const char *q=fields;*q;q++)g_req[rn++]=*q;
        char num[10];unsigned m=0,l=post_len;
        do{num[m++]=(char)('0'+l%10);l/=10;}while(l);
        while(m)g_req[rn++]=num[--m];
        g_req[rn++]='\r';g_req[rn++]='\n';
    }
    const char *tail = "Connection: close\r\nAccept-Encoding: identity\r\n\r\n";
    for (const char *q = tail; *q; q++) g_req[rn++] = *q;

    int sent = secure ? tls_write(g_req,rn) : net_tcp_send(sid,g_req,rn,timeout_ms);
    if (sent != (int)rn) { net_tcp_close(sid); return secure && sent < 0 ? sent : -4; }
    if(post_len&&tls_write(post_body,post_len)!=(int)post_len){net_tcp_close(sid);return -10;}

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
    r->form_policy=0;
    r->content_type[0]=0;
    for (;;) {
        ln = http_read_line(sid, timeout_ms);
        if (ln < 0) { net_tcp_close(sid); return -5; }
        if (ln == 0) break;                            /* 空行 = 头结束 */
        if (h_prefix_ci(g_line,"Content-Security-Policy:",24)) {
            r->form_policy|=13; /* unparsed header CSP: no script, image or CSS */
            /* No nonce/hash/source-list evaluator. Fail closed instead. */
            const char *p=g_line+24;
            while(*p){
                while(*p==' ' || *p=='\t' || *p==';') p++;
                if(h_prefix_ci(p,"form-action",11) || h_prefix_ci(p,"sandbox",7)) r->form_policy|=2;
                while(*p && *p!=';') p++;
            }
        } else if (header_value(g_line, "Content-Length", g_hval, H_TMP_VAL) == 0) {
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
        } else if(secure&&web_token&&header_value(g_line,"Set-Cookie",g_hval,H_TMP_VAL)==0){
            uint32_t now=0;
            if(!rtc_unix(&now))web_session_accept(web_token,host,port,path,g_hval,now);
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
    if(image_origin && !chunked && clen>=0 && (uint32_t)clen>outcap){
        net_tcp_close(sid);r->truncated=1;r->body_len=0;return 0;
    }
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

/* RFC 3986 relative path resolution, bounded by the caller's URL buffer.
 * Only HTTP(S); fragment-only navigation requires an HTML anchor model. */
static void normalize_http_path(char *url) {
    char *a=url;
    while(*a && *a!=':') a++;
    if(!*a) return;
    a+=3;  /* scheme://, ensured by the resolver */
    while(*a && *a!='/' && *a!='#' && *a!='?') a++;
    char *r=a, *w=a;
    while(*r=='/') {
        char *e=r+1;
        while(*e && *e!='/' && *e!='?' && *e!='#') e++;
        int tail=*e!='/';
        if(e-r==2 && r[1]=='.') {
            if(tail) *w++='/';
        } else if(e-r==3 && r[1]=='.' && r[2]=='.') {
            while(w>a && *--w!='/') {}
            if(tail) *w++='/';
        } else {
            while(r<e) *w++=*r++;
        }
        r=e;
    }
    while(*r && *r!='#') *w++=*r++;
    *w=0;
}

int http_resolve_url(const char *base, const char *loc, char *out, uint32_t cap) {
    while(*loc==' ' || *loc=='\t') loc++;
    uint32_t n=0, b=h_strlen(base), prefix=0, authority=0;
    while(loc[n] && loc[n]!='#') n++;
    while(n && (loc[n-1]==' ' || loc[n-1]=='\t')) n--;
    if (!n) return -1;
    if ((n>=7 && h_prefix_ci(loc,"http://",7)) || (n>=8 && h_prefix_ci(loc,"https://",8))) {
        if(h_copy_bounded(out,cap,loc,n)) return -1;
        normalize_http_path(out); return 0;
    }
    for (uint32_t i=0; i<n && loc[i]!='/' && loc[i]!='?'; i++) if (loc[i]==':') return -1;
    uint32_t sc=b>=8 && h_prefix_ci(base,"https://",8) ? 8 :
                b>=7 && h_prefix_ci(base,"http://",7) ? 7 : 0;
    if (!sc) return -1;
    authority=sc;
    while (authority<b && base[authority]!='/' && base[authority]!='?' && base[authority]!='#') authority++;
    int slash=0;
    if (loc[0]=='/' && loc[1]=='/') prefix=sc-2;
    else if (loc[0]=='/') prefix=authority;
    else {
        uint32_t end=authority;
        while (end<b && base[end]!='?' && base[end]!='#') end++;
        prefix=end;
        if (*loc!='?') while (prefix>authority && base[prefix-1]!='/') prefix--;
        slash=(prefix==authority);
    }
    if (prefix+n+(unsigned)slash>=cap) return -1;
    memcpy(out,base,prefix);
    if (slash) out[prefix++]='/';
    memcpy(out+prefix,loc,n);
    out[prefix+n]=0;
    normalize_http_path(out);
    return 0;
}

static int fetch_owned_url(const char *url, void *out, uint32_t outcap,
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
        if (secure && !https) return -14;
        if(post_len&&!https)return -14;
        cross_image=0;
        if(image_origin){
            unsigned i=0;
            while(image_origin[i] && !(i>=8 && (image_origin[i]=='/' || image_origin[i]=='?' || image_origin[i]=='#'))) i++;
            unsigned j=0;
            while(g_url[j] && !(j>=8 && (g_url[j]=='/' || g_url[j]=='?' || g_url[j]=='#'))) j++;
            cross_image=(i!=j || !h_prefix_ci(g_url,image_origin,i));
            if(cross_image && (!https || port!=443))return -14;
            if(style_resource && hop){
                unsigned n=0;
                while(url[n] && !(n>=8 && (url[n]=='/' || url[n]=='?' || url[n]=='#'))) n++;
                /* Meta CSP was evaluated on the initial URL. Do not extend
                 * that permission to another origin via redirects. */
                if(j!=n || !h_prefix_ci(g_url,url,n))return -14;
            }
        }
        secure = https;
        if (!http_remaining()) return -5;
        if (port == 0) return -1;

        char loc[HTTP_URL_MAX];
        int rc = http_round(g_host, port, g_path, out, outcap, timeout_ms, r,
                            loc, sizeof(loc));
        if (rc != 0) return rc;

        int redir = (r->status >= 300 && r->status < 400 && r->status != 304);
        if (!redir || loc[0] == '\0') {
            h_copy_bounded(r->final_url,sizeof(r->final_url),g_url,h_strlen(g_url));
            return 0;
        }

        /* 防环：跳数超上限即失败，不继续 */
        if (++hop > HTTP_MAX_REDIR) return -8;
        if (http_resolve_url(g_url, loc, g_rurl, sizeof(g_rurl)) != 0)
            return -8;
        unsigned a=0,b=0;
        while(g_url[a]&&!(a>=8&&(g_url[a]=='/'||g_url[a]=='?'||g_url[a]=='#')))a++;
        while(g_rurl[b]&&!(b>=8&&(g_rurl[b]=='/'||g_rurl[b]=='?'||g_rurl[b]=='#')))b++;
        int same=a==b&&h_prefix_ci(g_url,g_rurl,a);
        if(!same)send_cookies=0;
        if(post_len){
            if(r->status==301||r->status==302||r->status==303){post_len=0;post_body=0;}
            else if((r->status!=307&&r->status!=308)||!same)return -14;
        }
        r->redirects = hop;
        /* 下一轮用新 URL */
        for (uint32_t i = 0; i < sizeof(g_url); i++) {
            g_url[i] = g_rurl[i];
            if (!g_rurl[i]) break;
        }
    }
}

static int fetch_policy(const char *url,const char *origin,void *out,uint32_t cap,uint32_t timeout,http_result_t *r,int (*cancelled)(void),int style,
                        uint32_t token,int cookies,const uint8_t *body,uint32_t length) {
    if (!r || !timeout || timeout > 60000) return -1;
    memset(r,0,sizeof(*r));
    if (__atomic_exchange_n(&fetch_owned,1,__ATOMIC_ACQUIRE)) return -9;
    deadline=g_pit_ticks+(origin?90000:timeout*4);
    secure=0;
    image_origin=origin; image_cancelled=cancelled; cross_image=0;
    style_resource=style;
    web_token=token;send_cookies=cookies;post_body=body;post_len=length;
    int rc=fetch_owned_url(url,out,cap,timeout,r);
    if (secure) tls_forget();
    if(token){auth_wipe(g_req,sizeof(g_req));auth_wipe(g_line,sizeof(g_line));
        auth_wipe(g_hval,sizeof(g_hval));auth_wipe(g_rxbuf,sizeof(g_rxbuf));}
    web_token=post_len=0;post_body=0;
    image_origin=0; image_cancelled=0; cross_image=0;
    __atomic_store_n(&fetch_owned,0,__ATOMIC_RELEASE);
    return rc;
}
int http_fetch(const char *url,void *out,uint32_t cap,uint32_t timeout,http_result_t *r){
    return fetch_policy(url,0,out,cap,timeout,r,0,0,0,0,0,0);
}
int http_fetch_image(const char *url,const char *origin,void *out,uint32_t cap,uint32_t timeout,http_result_t *r,int (*cancelled)(void)){
    if(!origin || !*origin)return -1;
    return fetch_policy(url,origin,out,cap,timeout,r,cancelled,0,0,0,0,0);
}
int http_fetch_style(const char *url,const char *origin,void *out,uint32_t cap,uint32_t timeout,http_result_t *r,int (*cancelled)(void)){
    return fetch_policy(url,origin,out,cap,timeout,r,cancelled,1,0,0,0,0);
}
int http_fetch_web(const char *url,void *out,uint32_t cap,uint32_t timeout,http_result_t *r,
                   unsigned token,int cookies,const void *body,unsigned n,int (*cancelled)(void)){
    if(n&&(!body||!token))return -1;
    return fetch_policy(url,0,out,cap,timeout,r,cancelled,0,token,cookies,body,n);
}
