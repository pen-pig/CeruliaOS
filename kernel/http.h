/*
 * http.c - HTTP/1.1 客户端
 *
 * 定位：net.c 的 TCP 之上的应用层。net.c 只有服务端（net_httpd_once，
 * 响应一次就关），这里是唯一的客户端实现，供 shell 的 fetch/wget 与
 * 后续 HTML 渲染共用。
 *
 * 范围（刻意收窄，缺什么就明说，不做半吊子降级）：
 *   ✓ http:// 明文；GET；Host 头；Connection: close
 *   ✓ Content-Length 与 chunked 两种响应体
 *   ✓ 301/302/303/307/308 重定向（相对/绝对 URL，有环与跳数上限）
 *   ✓ 分块传输的逐块长度解析（含 ;ext 扩展丢弃）
 *   ✗ HTTPS/TLS  —— 没有 TLS 就没有 HTTPS。遇到 https:// 直接报错，
 *     绝不"去掉 s 降级成 http"冒充成功（那会把请求明文发出去还报错成功）。
 *   ✗ 分块编码的 Trailer 头 —— 直接忽略，不影响正文。
 *   ✗ keep-alive 复用 —— 一律 Connection: close，取完就关。
 *
 * 为什么不用系统调用：内核态拿不到用户指针语义（见 net.c 里
 * net_tcp_connect 上方的说明），这里直接调内核内部 API。
 */
#ifndef EZ_HTTP_H
#define EZ_HTTP_H

#include "types.h"

#define HTTP_MAX_REDIR   5      /* 重定向跳数上限（防环） */
#define HTTP_MAX_HDRS    24     /* 保存的头字段条数上限 */
#define HTTP_URL_MAX     256    /* URL 缓冲 */
#define HTTP_HOST_MAX    128    /* 主机名缓冲 */
#define HTTP_PATH_MAX    384    /* 路径+查询缓冲 */

/* fetch 的结果码。0 成功，其余见 http_fetch() 注释。 */
typedef struct {
    int32_t  status;            /* HTTP 状态码（如 200；无则 0）      */
    uint32_t content_length;    /* Content-Length；无 / chunked 则 0   */
    uint32_t body_len;          /* 实际写入 out 的字节数               */
    uint32_t redirects;         /* 跟随的重定向次数                   */
    uint8_t  chunked;           /* 1 = 响应体是 chunked               */
    uint8_t  truncated;         /* 1 = 超出 out 容量，被截断           */
    char     content_type[48];  /* Content-Type 的值（截断保护）      */
} http_result_t;

/*
 * 取一个 URL 的响应体，写入 out。
 *   url        "http://host[:port]/path"（也接受裸 "host/path"，默认 80）
 *   out/outcap 响应体缓冲；容量不够时**截断并置 truncated=1**（不报错），
 *              因为网页普遍大于任何合理缓冲，让调用方自己决定够不够。
 *   timeout_ms 单次 socket 操作超时，不是总时限。
 * 返回 0 = 拿到完整响应体；其余见下（失败时 *r 的字段无意义）。
 *   -1 参数非法/内存不足   -2 DNS 失败      -3 连接失败
 *   -4 请求发送失败        -5 响应格式非法  -6 协议错误（如状态行不合法）
 *   -7 不支持的 scheme（https 等）
 *   -8 重定向超限或成环
 */
int http_fetch(const char *url, void *out, uint32_t outcap,
               uint32_t timeout_ms, http_result_t *r);

/* URL 拆分：只做语法切分，不联网。host 若为 IP 字面量则原样保留。
 * 成功返回 0。fail closed：任何越界/畸形输入返回 -1。 */
int http_parse_url(const char *url, char *host, uint32_t hostcap,
                   uint16_t *port, char *path, uint32_t pathcap,
                   uint8_t *is_https);

#endif