/* Freestanding user-mode shims for QuickJS (no POSIX/file/network library). */
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <sys/time.h>
#include <stdint.h>
extern void _exit(int) __attribute__((noreturn));
_Noreturn void abort(void){_exit(125);}
int abs(int a){return a<0?-a:a;}
long labs(long a){return a<0?-a:a;}
static int web_clock(uint32_t *s){
    int r;__asm__ volatile("int $0x80":"=a"(r):"0"(106),"b"(s):"memory");return r;
}
int gettimeofday(struct timeval *t,void *zone){
    uint32_t sec=0;(void)zone;int r=web_clock(&sec);
    t->tv_sec=sec;t->tv_usec=0;return r;
}
int clock_gettime(int which,struct timespec *t){
    (void)which;struct timeval u;
    int r=gettimeofday(&u,0);t->tv_sec=u.tv_sec;t->tv_nsec=0;return r;
}
struct tm *localtime_r(const time_t *time,struct tm *out){(void)time;out->tm_gmtoff=0;return out;}

typedef union {size_t n;uint64_t align;} alloc_header;
void *web_alloc(size_t n){
    if(n>SIZE_MAX-sizeof(alloc_header))return 0;
    alloc_header *p=malloc(n+sizeof(*p));if(!p)return 0;p->n=n;return p+1;
}
size_t web_usable(const void *p){return p?((const alloc_header*)p)[-1].n:0;}
void web_free(void *p){if(p)free((alloc_header*)p-1);}
void *web_realloc(void *p,size_t n){
    if(!n){web_free(p);return 0;}
    size_t old=web_usable(p);
    if(p&&old>=n)return p;
    void *q=web_alloc(n);if(!q)return 0;
    if(old)memcpy(q,p,old);
    web_free(p);return q;
}
void *realloc(void *p,size_t n){return web_realloc(p,n);}
void *calloc(size_t n,size_t m){
    if(n&&m>SIZE_MAX/n)return 0;
    void *p=web_alloc(n*m);if(p)memset(p,0,n*m);return p;
}
void *memchr(const void *p,int v,size_t n){
    const unsigned char *s=p;
    while(n--){if(*s==(unsigned char)v)return (void*)s;s++;}return 0;
}
int memcmp(const void *p,const void *q,size_t n){
    const unsigned char *a=p,*b=q;
    for(size_t i=0;i<n;i++)if(a[i]!=b[i])return a[i]-b[i];return 0;
}
void *memmove(void *p,const void *q,size_t n){
    unsigned char *d=p;const unsigned char *s=q;
    if((uintptr_t)d<(uintptr_t)s)return memcpy(p,q,n);
    if(d!=s)while(n){n--;d[n]=s[n];}return p;
}
char *strchr(const char *s,int c){while(*s&&*s!=(char)c)s++;return *s==(char)c?(char*)s:0;}
char *strrchr(const char *s,int c){
    const char *p=0;do{if(*s==(char)c)p=s;}while(*s++);return (char*)p;
}
char *strstr(const char *s,const char *key){
    size_t n=strlen(key);if(!n)return (char*)s;
    while(*s){if(!strncmp(s,key,n))return (char*)s;s++;}return 0;
}
char *strcpy(char *d,const char *s){return memcpy(d,s,strlen(s)+1);}
char *strdup(const char *s){size_t n=strlen(s)+1;char *p=web_alloc(n);return p?memcpy(p,s,n):0;}

static void emit(char *out,size_t cap,size_t *n,char c){
    if(*n<cap&&cap-*n>1)out[*n]=c;
    if(*n<0x7fffffffu)(*n)++;
}
int vsnprintf(char *out,size_t cap,const char *fmt,va_list ap){
    size_t n=0;
    while(*fmt){
        char c=*fmt++;
        if(c!='%'){emit(out,cap,&n,c);continue;}
        int zero=*fmt=='0';if(zero)fmt++;
        unsigned w=0;
        while(*fmt>='0'&&*fmt<='9'){if(w<10000)w=w*10+(*fmt-'0');fmt++;}
        int precision=-1;
        if(*fmt=='.'){
            fmt++;precision=0;
            if(*fmt=='*'){precision=va_arg(ap,int);fmt++;}
            else while(*fmt>='0'&&*fmt<='9'){if(precision<10000)precision=precision*10+(*fmt-'0');fmt++;}
        }
        unsigned wide=0;
        if(*fmt=='l'){fmt++;wide=1;if(*fmt=='l'){fmt++;wide=2;}}
        else if(*fmt=='z'){fmt++;wide=1;}
        if(!*fmt)break;
        c=*fmt++;
        if(c=='s'){
            const char *s=va_arg(ap,const char*);if(!s)s="(null)";
            while(*s&&(precision!=0)){emit(out,cap,&n,*s++);if(precision>0)precision--;}
        } else if(c=='c')emit(out,cap,&n,(char)va_arg(ap,int));
        else if(c=='%')emit(out,cap,&n,'%');
        else if(c=='d'||c=='i'||c=='u'||c=='x'||c=='X'||c=='p'){
            uint64_t v;int neg=0;
            if(c=='d'||c=='i'){
                int64_t j=wide==2?va_arg(ap,long long):wide==1?va_arg(ap,long):va_arg(ap,int);
                neg=j<0;v=neg?0-(uint64_t)j:(uint64_t)j;
            } else if(c=='p')v=(uintptr_t)va_arg(ap,void*);
            else v=wide==2?va_arg(ap,unsigned long long):wide==1?va_arg(ap,unsigned long):va_arg(ap,unsigned);
            unsigned base=(c=='x'||c=='X'||c=='p')?16:10,k=0;char rev[24];
            do{unsigned d=v%base;rev[k++]=(char)(d<10?'0'+d:(c=='X'?'A':'a')+d-10);v/=base;}while(v);
            unsigned pad=w>k+(unsigned)neg?w-k-(unsigned)neg:0;
            if(neg&&zero)emit(out,cap,&n,'-');
            while(pad--)emit(out,cap,&n,zero?'0':' ');
            if(neg&&!zero)emit(out,cap,&n,'-');
            while(k)emit(out,cap,&n,rev[--k]);
        } else if(c=='f'||c=='e'||c=='g'){(void)va_arg(ap,double);emit(out,cap,&n,'?');}
    }
    if(cap)out[n<cap?n:cap-1]=0;
    return (int)n;
}
int snprintf(char *out,size_t cap,const char *fmt,...){
    va_list ap;va_start(ap,fmt);int r=vsnprintf(out,cap,fmt,ap);va_end(ap);return r;
}
