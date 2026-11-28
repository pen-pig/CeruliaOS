/* Conservative RFC 6265 cookie jar. Transport calls it only after verified
 * HTTPS. Domain attributes are checked but stored host-only, port-scoped.
 * No persistence, cookie scripts, public suffix exceptions or third parties. */
#include "websession.h"
#include "auth.h"
#include "irqflags.h"
#include "kmalloc.h"
#include "mem.h"

typedef struct {const char *s;unsigned n;} part;
typedef struct cookie {
    struct cookie *next;
    uint32_t expires,bytes;
    uint16_t port,hn,pn,nn,vn;
    char data[]; /* host | path | name | value, compact, not NUL-delimited */
} cookie;
static cookie *head;
static uint32_t generation=1;
static unsigned len(const char *s){unsigned n=0;while(s[n])n++;return n;}
static char lc(char c){return c>='A'&&c<='Z'?c+32:c;}
static int space(char c){return c==' '||c=='\t';}
static part trim(part p){while(p.n&&space(*p.s)){p.s++;p.n--;}while(p.n&&space(p.s[p.n-1]))p.n--;return p;}
static int eq(part p,const char *s,unsigned n,int insensitive){
    if(p.n!=n)return 0;
    for(unsigned i=0;i<n;i++)if(insensitive?lc(p.s[i])!=lc(s[i]):p.s[i]!=s[i])return 0;
    return 1;
}
static int named(part p,const char *s){return eq(p,s,len(s),1);}
static int begin(part p,const char *s){
    unsigned n=len(s);return p.n>=n&&!memcmp(p.s,s,n);
}
static part split(part *p,char sep) {
    unsigned n=0;while(n<p->n&&p->s[n]!=sep)n++;
    part r={p->s,n};
    unsigned used=n+(n<p->n);p->s+=used;p->n-=used;return r;
}
static int name_valid(part p){
    if(!p.n)return 0;
    for(unsigned i=0;i<p.n;i++){
        unsigned char c=p.s[i];
        if(c<=32||c>=127)return 0;
        for(const char *bad="()<>@,;:\\\"/[]?={}";*bad;bad++)if(c==(unsigned char)*bad)return 0;
    }
    return 1;
}
static int value_valid(part p){
    unsigned a=0,b=p.n;
    if(b>=2&&p.s[0]=='"'&&p.s[b-1]=='"'){a++;b--;}
    for(unsigned i=a;i<b;i++){
        unsigned char c=p.s[i];
        if(c<=32||c>=127||c=='"'||c==','||c==';'||c=='\\')return 0;
    }
    return 1;
}
static int domain_valid(part p,const char *host,unsigned hn){
    if(p.n&&p.s[0]=='.'){p.s++;p.n--;}
    if(!p.n||p.n>hn||p.s[p.n-1]=='.')return 0;
    unsigned off=hn-p.n;
    return (!off||host[off-1]=='.')&&eq(p,host+off,p.n,1);
}
static int digits(part p,unsigned *out){
    if(!p.n)return 0;
    unsigned v=0;
    for(unsigned i=0;i<p.n;i++){
        unsigned c=p.s[i]-'0';if(c>9)return 0;
        v=v>429496728u?0xffffffffu:v*10+c;
    }
    *out=v;return 1;
}
static int leap(unsigned y){return !(y%4)&&(y%100||!(y%400));}
static unsigned monthdays(unsigned m,unsigned y){
    static const uint8_t d[]={31,28,31,30,31,30,31,31,30,31,30,31};
    return d[m-1]+(m==2&&leap(y));
}
static int sep(char c){return c==' '||c=='\t'||c==','||c=='-'||c=='/';}
static uint32_t date(part p){
    const char *months[]={"jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"};
    unsigned y=0,m=0,d=0,h=0,mi=0,sec=0;int clock=0;
    for(unsigned i=0;i<p.n;){
        while(i<p.n&&sep(p.s[i]))i++;
        unsigned j=i;while(j<p.n&&!sep(p.s[j]))j++;
        part w={p.s+i,j-i};i=j;
        part t=w,a=split(&t,':');
        if(t.n){
            part b=split(&t,':');
            if(!clock&&digits(a,&h)&&digits(b,&mi)&&digits(t,&sec))clock=1;
        } else {
            unsigned n;
            if(digits(w,&n)){if(w.n<=2&&!d)d=n;else if(!y)y=n<70?n+2000:n<100?n+1900:n;}
            else if(!m)for(unsigned k=0;k<12;k++)if(named(w,months[k])){m=k+1;break;}
        }
    }
    if(!y||!m||!d||!clock||h>23||mi>59||sec>59||d>monthdays(m,y))return 0;
    if(y<1970)return 1;
    if(y>2106)return 0xffffffffu;
    uint64_t days=d-1;
    for(unsigned k=1970;k<y;k++)days+=365+leap(k);
    for(unsigned k=1;k<m;k++)days+=monthdays(k,y);
    uint64_t result=days*86400+h*3600+mi*60+sec;
    return result>0xffffffffu?0xffffffffu:result?(uint32_t)result:1;
}
static void drop(cookie **p){
    cookie *c=*p;*p=c->next;auth_wipe(c,c->bytes);kfree(c);
}
uint32_t web_session_token(void){return __atomic_load_n(&generation,__ATOMIC_RELAXED);}
void web_session_clear(void){
    unsigned irq=irq_save_disable();
    uint32_t next=generation+1;
    __atomic_store_n(&generation,next?next:1,__ATOMIC_RELAXED);
    while(head)drop(&head);
    irq_restore(irq);
}
void web_session_accept(uint32_t token,const char *host,uint16_t port,const char *path,
                        const char *set_cookie,uint32_t now){
    if(!token||token!=web_session_token())return;
    part rest={set_cookie,len(set_cookie)},pair=split(&rest,';'),name=trim(split(&pair,'=')),value=trim(pair);
    if(!name_valid(name)||!value_valid(value))return;
    unsigned had_equal=0;
    for(const char *p=set_cookie;*p&&*p!=';';p++)if(*p=='='){had_equal=1;break;}
    if(!had_equal)return;
    unsigned hn=len(host),pl=0;
    for(unsigned i=1;path[i]&&path[i]!='?';i++)if(path[i]=='/')pl=i;
    part where={path,pl?pl:1};
    uint32_t expires=0;
    unsigned max_age=0;int from_age=0,erase=0,has_secure=0,has_domain=0,explicit_path=0;
    while(rest.n){
        part field=split(&rest,';'),key=trim(split(&field,'=')),val=trim(field);
        if(named(key,"secure"))has_secure=1;
        else if(named(key,"domain")){
            if(!domain_valid(val,host,hn))return;
            has_domain=1;
        } else if(named(key,"path")&&val.n&&val.s[0]=='/'){where=val;explicit_path=1;}
        else if(named(key,"max-age")){
            int neg=val.n&&val.s[0]=='-';if(neg){val.s++;val.n--;}
            if(digits(val,&max_age)){from_age=1;erase=neg||!max_age;}
        } else if(named(key,"expires"))expires=date(val);
    }
    if(begin(name,"__Secure-")&&!has_secure)return;
    if(begin(name,"__Host-")&&(!has_secure||has_domain||!explicit_path||where.n!=1))return;
    if(from_age){expires=erase?1:max_age>0xffffffffu-now?0xffffffffu:now+max_age;}
    if(expires&&expires<=now)erase=1;
    if(hn+where.n+name.n+value.n>4096)return;
    unsigned irq=irq_save_disable();
    if(token==generation){
        cookie **pos=&head;
        while(*pos){
            cookie *c=*pos;
            if(c->expires&&c->expires<=now){drop(pos);continue;}
            if(c->port==port&&eq((part){host,hn},c->data,c->hn,1)&&
               eq(where,c->data+c->hn,c->pn,0)&&eq(name,c->data+c->hn+c->pn,c->nn,0)){
                drop(pos);break;
            }
            pos=&c->next;
        }
        if(!erase){
            unsigned bytes=sizeof(cookie)+hn+where.n+name.n+value.n;
            cookie *c=kmalloc(bytes);
            if(c){
                *c=(cookie){.expires=expires,.bytes=bytes,.port=port,.hn=hn,.pn=where.n,.nn=name.n,.vn=value.n};
                memcpy(c->data,host,hn);memcpy(c->data+hn,where.s,where.n);
                memcpy(c->data+hn+where.n,name.s,name.n);memcpy(c->data+hn+where.n+name.n,value.s,value.n);
                pos=&head;
                while(*pos&&(*pos)->pn>=c->pn)pos=&(*pos)->next;
                c->next=*pos;*pos=c;
            }
        }
    }
    irq_restore(irq);
}
int web_session_header(uint32_t token,const char *host,uint16_t port,const char *path,
                      char *out,unsigned cap,uint32_t now){
    if(!token)return 0;
    unsigned hn=len(host),pn=0,used=0;while(path[pn]&&path[pn]!='?')pn++;
    unsigned irq=irq_save_disable();
    if(token!=generation){irq_restore(irq);return 0;}
    for(cookie **pos=&head;*pos;){
        cookie *c=*pos;
        if(c->expires&&c->expires<=now){drop(pos);continue;}
        pos=&c->next;
        const char *p=c->data+c->hn;
        if(c->port!=port||!eq((part){host,hn},c->data,c->hn,1)||pn<c->pn||memcmp(path,p,c->pn)||
           (pn!=c->pn&&p[c->pn-1]!='/'&&path[c->pn]!='/'))continue;
        unsigned needed=c->nn+c->vn+1+(used?2:0);
        if(needed>cap-used){irq_restore(irq);return -1;}
        if(used){out[used++]=';';out[used++]=' ';}
        memcpy(out+used,p+c->pn,c->nn);used+=c->nn;
        out[used++]='=';
        memcpy(out+used,p+c->pn+c->nn,c->vn);used+=c->vn;
    }
    irq_restore(irq);
    return (int)used;
}
