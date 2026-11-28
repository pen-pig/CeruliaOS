/* Guest TCP + RTC adapter. HTTP serializes ownership of this TLS engine. */
#include "bearssl.h"
#include <string.h>
#include "roots.h"
extern int net_tcp_recv(int, void *, unsigned, unsigned);
extern int net_tcp_send(int, const void *, unsigned, unsigned);
extern int rtc_unix(unsigned *);
extern unsigned http_remaining(void);
extern void auth_wipe(void *, unsigned);
static br_ssl_client_context client;
static br_x509_minimal_context chain;
static br_sslio_context stream;
static unsigned char records[BR_SSL_BUFSIZE_BIDI];
static int socket_id;

/* Not supplied by the guest's minimal C runtime. */
size_t strlen(const char *s) { size_t n=0; while (s[n]) n++; return n; }
void *memmove(void *dst, const void *src, size_t n) {
    unsigned char *d=dst; const unsigned char *s=src;
    if ((uintptr_t)d <= (uintptr_t)s) { for (size_t i=0;i<n;i++) d[i]=s[i]; }
    else { while(n) { --n; d[n]=s[n]; } }
    return dst;
}

static int receive(void *ctx, unsigned char *buf, size_t n) {
    (void)ctx; unsigned t=http_remaining();
    int r=t ? net_tcp_recv(socket_id, buf, n, t) : -1;
    return r > 0 ? r : -1; /* raw FIN is not an authenticated close_notify */
}
static int transmit(void *ctx, const unsigned char *buf, size_t n) {
    (void)ctx; unsigned t=http_remaining();
    return t ? net_tcp_send(socket_id, buf, n < 1024 ? n : 1024, t) : -1;
}
static int entropy(unsigned char *out) {
    unsigned a=1,b,c,d;
    __asm__ volatile("cpuid" : "+a"(a),"=b"(b),"=c"(c),"=d"(d));
    if (!(c & (1u<<30))) return 0;
    for (int i=0;i<32;i+=4) {
        unsigned v; unsigned char ok=0;
        for (int j=0;j<10 && !ok;j++)
            __asm__ volatile("rdrand %0; setc %1" : "=&r"(v),"=qm"(ok) :: "cc");
        if (!ok) return 0;
        memcpy(out+i,&v,4);
    }
    return 1;
}

int tls_start(int sid, const char *host) {
    unsigned now; unsigned char seed[32];
    static const uint16_t suites[] = {
        BR_TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256, BR_TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256,
        BR_TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256, BR_TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256,
        BR_TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384, BR_TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384
    };
    if (rtc_unix(&now)) return -11;
    if (!entropy(seed)) { auth_wipe(seed,sizeof(seed)); return -12; }
    socket_id=sid;
    br_ssl_client_init_full(&client,&chain,anchors,sizeof(anchors)/sizeof(anchors[0]));
    br_ssl_engine_set_versions(&client.eng,BR_TLS12,BR_TLS12);
    br_ssl_engine_set_suites(&client.eng,suites,sizeof(suites)/sizeof(suites[0]));
    br_x509_minimal_set_hash(&chain,br_sha1_ID,NULL);
    br_x509_minimal_set_time(&chain,now/86400u+719528u,now%86400u);
    br_ssl_engine_set_buffer(&client.eng,records,sizeof(records),1);
    br_ssl_engine_inject_entropy(&client.eng,seed,sizeof(seed));
    auth_wipe(seed,sizeof(seed));
    if (!br_ssl_client_reset(&client,host,0)) return -10;
    br_sslio_init(&stream,&client.eng,receive,NULL,transmit,NULL);
    if (br_sslio_flush(&stream)) {
        int err=br_ssl_engine_last_error(&client.eng);
        return err>=BR_ERR_X509_INVALID_VALUE && err<=BR_ERR_X509_NOT_TRUSTED ? -13 : -10;
    }
    /* Minimal X.509 matches DNS names, not iPAddress SANs. Never authorize
     * a numeric authority via CN/dNSName, even with an otherwise valid chain. */
    size_t p=0;
    while (host[p]=='.' || (host[p]>='0' && host[p]<='9')) p++;
    if (!host[p]) return -15;
    return 0;
}
int tls_write(const void *buf, unsigned n) {
    if (br_sslio_write_all(&stream,buf,n) || br_sslio_flush(&stream)) {
        return -10;
    }
    return (int)n;
}
int tls_read(void *buf, unsigned n) {
    int r=br_sslio_read(&stream,buf,n);
    if (r < 0 && !br_ssl_engine_last_error(&client.eng)) return 0;
    return r;
}
void tls_forget(void) {
    auth_wipe(&client,sizeof(client)); auth_wipe(records,sizeof(records));
    auth_wipe(&chain,sizeof(chain)); auth_wipe(&stream,sizeof(stream));
}
