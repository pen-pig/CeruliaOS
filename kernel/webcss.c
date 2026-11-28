/* Native author styles: collect HTML scopes, then resolve rules in source order.
 * This is a bounded-feature layout engine, not a script interpreter. */
#include "webcss.h"
#include "pmm.h"
#include "mem.h"
#include "http.h"
#include "weburl.h"
#include "gfx.h"
#include "webview.h"

typedef struct { const char *p; unsigned n; } str;
static str span(const void *p,unsigned n){return (str){p,n};}
static str sub(str s,unsigned a,unsigned b){return span(s.p+a,b-a);}
static int ws(char c){return c==' '||c=='\t'||c=='\n'||c=='\f'||c=='\r';}
static char lower(char c){return c>='A'&&c<='Z'?c+('a'-'A'):c;}
static int ident(char c){return (unsigned char)c>=128 || (c>='a'&&c<='z') || (c>='A'&&c<='Z') ||
                              (c>='0'&&c<='9') || c=='-' || c=='_';}
static int eq(str a,str b,int ci){
    if(a.n!=b.n)return 0;
    for(unsigned i=0;i<a.n;i++)if(ci?lower(a.p[i])!=lower(b.p[i]):a.p[i]!=b.p[i])return 0;
    return 1;
}
static int lit(str a,const char *b){unsigned n=0;while(b[n])n++;return eq(a,span(b,n),1);}
static str trim(str s){while(s.n&&ws(*s.p)){s.p++;s.n--;}while(s.n&&ws(s.p[s.n-1]))s.n--;return s;}
static str word(str *s){
    *s=trim(*s);unsigned i=0;while(i<s->n&&!ws(s->p[i]))i++;
    str w=sub(*s,0,i);*s=sub(*s,i,s->n);return w;
}
static int has_word(str a,str s,int ci){while(a.n)if(eq(word(&a),s,ci))return 1;return 0;}
static str attr(str s,const char *name){
    unsigned i=0;
    while(i<s.n){
        while(i<s.n && (ws(s.p[i])||s.p[i]=='/'))i++;
        unsigned a=i;
        while(i<s.n&&!ws(s.p[i])&&s.p[i]!='=')i++;
        str key=sub(s,a,i);
        while(i<s.n&&ws(s.p[i]))i++;
        str value={0};
        if(i<s.n&&s.p[i]=='='){
            i++;while(i<s.n&&ws(s.p[i]))i++;
            char q=i<s.n&&(s.p[i]=='"'||s.p[i]=='\'')?s.p[i++]:0;
            a=i;while(i<s.n&&(q?s.p[i]!=q:!ws(s.p[i])))i++;
            value=sub(s,a,i);
            if(q&&i<s.n)i++;
        }
        if(lit(key,name))return value;
    }
    return (str){0};
}

/* All metadata grows with available pages. Navigation releases its arenas. */
struct chunk {struct chunk *next;unsigned used;};
static struct chunk *arena;
static void *alloc(unsigned bytes){
    bytes=(bytes+7)&~7u;
    if(bytes>4096-sizeof(struct chunk))return 0;
    if(!arena||arena->used+bytes>4096){
        struct chunk *c=(void*)pmm_alloc_page();
        if(!c)return 0;
        c->next=arena;c->used=sizeof(*c);arena=c;
    }
    void *p=(char*)arena+arena->used;arena->used+=bytes;memset(p,0,bytes);return p;
}
typedef struct el {
    struct el *parent,*next,*prev,*opening,*outer;
    struct el *child,*child_tail,*sibling;
    str tag,id,classes,inline_style,background;
    const char *background_base;
    struct asset *image;
    css_style style;
    uint32_t paint_bg;
    unsigned score[17];
    unsigned w,h,min_h,fit,center,center_box,from,to;
    unsigned display,column,spacing,visibility;
    int x,y,box_w,box_h,used,items_x,items_h,item_gap,row_space,auto_width,share_more,shrink_sum;
} el;
/* The node index is shared with the upstream HTML display arena. */
#define NODE_GROUP 256
struct marks {struct marks *next; el *items[NODE_GROUP],*starts[NODE_GROUP];};
static struct marks *maps,*mark_tail;
static unsigned mark_used;
typedef struct sheet {
    struct sheet *next;
    str text,ref,media;
    unsigned pages,clean;
    const char *base;
} sheet;
typedef struct policy {struct policy *next;str text;} policy;
static el *first,*last,*owner,*scope;
static sheet *sheets,*tail,*next,*pending;
static policy *policies;
static int policy_oom,mark_oom;
static unsigned fetched;
typedef struct asset {struct asset *next;const char *url;web_bitmap bmp;unsigned length,attempts;int requested;} asset;
static asset *assets,*background_pending;
static el *flow;
static const char *doc_url;
static int view_width,view_height;
static unsigned root_size;
static const css_style base_style={CSS_UNSET,CSS_UNSET,0,0,0,0};

void css_clear(void){
    for(asset *a=assets;a;a=a->next)web_free_image(&a->bmp);
    for(sheet *s=sheets;s;s=s->next)if(s->pages)pmm_free_pages((uint32_t)s->text.p,s->pages);
    while(arena){struct chunk *n=arena->next;pmm_free_page((uint32_t)arena);arena=n;}
    maps=mark_tail=0;mark_used=0;
    first=last=owner=scope=0;sheets=tail=next=pending=0;policies=0;policy_oom=mark_oom=0;fetched=0;
    assets=background_pending=0;flow=0;doc_url=0;
}
void css_document(const char *final_url){doc_url=final_url;}
static void add_sheet(str text,str ref,str media){
    sheet *s=alloc(sizeof(*s));if(!s)return;
    s->text=text;s->ref=ref;s->media=media;
    if(tail)tail->next=s;else sheets=s;
    tail=s;
    if(!next)next=s;
}
void css_html(unsigned event,const uint8_t *a,unsigned an,const uint8_t *b,unsigned bn){
    str name=span(a,an),attrs=span(b,bn);
    if(event==7){
        str src=attr(attrs,"src"),type=trim(attr(attrs,"type"));
        if(src.n&&(!type.n||lit(type,"text/javascript")||lit(type,"application/javascript"))&&
           !attr(attrs,"nomodule").p&&!attr(attrs,"integrity").p)
            wv_script_register((const uint8_t *)src.p,src.n);
        return;
    }
    if(event==0){
        if(mark_oom)return;
        if(!mark_tail||mark_used==NODE_GROUP){
            struct marks *m=alloc(sizeof(*m));if(!m){mark_oom=1;return;}
            if(mark_tail)mark_tail->next=m;else maps=m;
            mark_tail=m;mark_used=0;
        }
        mark_tail->items[mark_used++]=owner;
        for(el *e=owner;e;e=e->parent){
            if(!e->to)e->from=an;
            e->to=an+1;
        }
        return;
    }
    if(event==5){owner=scope=0;return;}
    if(event==6){owner=scope;return;}
    if(event==2){
        for(el *e=scope;e;e=e->parent)if(eq(e->tag,name,1)){scope=e->parent;break;}
        owner=scope;return;
    }
    if(event==4){
        str type=attr(attrs,"type");
        if(!type.n||lit(type,"text/css"))add_sheet(name,(str){0},attr(attrs,"media"));
        return;
    }
    if(lit(name,"link")){
        str ref=attr(attrs,"href"),type=attr(attrs,"type"),rel=attr(attrs,"rel");
        if(ref.n && has_word(rel,span("stylesheet",10),1) && !has_word(rel,span("alternate",9),1) &&
           (!type.n||lit(type,"text/css")))add_sheet((str){0},ref,attr(attrs,"media"));
    }
    if(lit(name,"meta")&&lit(attr(attrs,"http-equiv"),"content-security-policy")){
        policy *p=alloc(sizeof(*p));if(p){p->text=attr(attrs,"content");p->next=policies;policies=p;}
        else policy_oom=1;
    }
    el *e=alloc(sizeof(*e));if(!e)return;
    e->tag=name;e->id=attr(attrs,"id");e->classes=attr(attrs,"class");e->inline_style=attr(attrs,"style");
    e->parent=scope;
    if(scope){
        if(scope->child_tail)scope->child_tail->sibling=e;else scope->child=e;
        scope->child_tail=e;
    }
    e->prev=last;
    if(last)last->next=e;else first=e;
    last=e;
    owner=e;
    if(event==1)scope=e;
}
static void authority(const char *url,str *scheme,str *host){
    unsigned i=0;while(url[i]&&url[i]!=':')i++;
    *scheme=span(url,i);
    if(url[i]!=':'||url[i+1]!='/'||url[i+2]!='/'){*host=(str){0};return;}
    unsigned j=i+=3;
    while(url[j]&&url[j]!='/'&&url[j]!='?'&&url[j]!='#')j++;
    *host=span(url+i,j-i);
}
static int origins(const char *a,const char *b){
    str as,ah,bs,bh;authority(a,&as,&ah);authority(b,&bs,&bh);
    return ah.n&&eq(ah,bh,1)&&eq(as,bs,1);
}
static str directive(str policy_text,const char *key){
    unsigned i=0;
    while(i<policy_text.n){
        unsigned j=i;while(j<policy_text.n&&policy_text.p[j]!=';')j++;
        str d=sub(policy_text,i,j);
        if(lit(word(&d),key))return d;
        i=j+1;
    }
    return (str){0};
}
static int allowed(const char *base,const char *url,int inl,int attr_style){
    if(policy_oom)return 0;
    for(policy *p=policies;p;p=p->next){
        for(unsigned i=0;i<p->text.n;i++)if(p->text.p[i]=='&'||p->text.p[i]==',')return 0;
        str d=directive(p->text,attr_style?"style-src-attr":"style-src-elem");
        if(!d.p)d=directive(p->text,"style-src");
        if(!d.p)d=directive(p->text,"default-src");
        if(!d.p)continue;
        int good=0;
        while(d.n){
            str s=word(&d);
            if(inl){
                if((s.n>=7 && lit(sub(s,0,7),"'nonce-"))||(s.n>=4 && lit(sub(s,0,4),"'sha")))return 0;
                if(lit(s,"'unsafe-inline'"))good=1;
                continue;
            }
            str sc,host;authority(url,&sc,&host);
            if(!host.n)continue;
            if(lit(s,"'self'")&&origins(base,url))good=1;
            if(lit(s,"*")|| (lit(s,"https:")&&lit(sc,"https")))good=1;
            if(lit(sc,"https")&&eq(s,host,1))good=1;
            if(lit(sc,"https")&&s.n>2&&s.p[0]=='*'&&s.p[1]=='.'&&host.n>s.n-1&&
               eq(sub(s,1,s.n),sub(host,host.n-s.n+1,host.n),1))good=1;
        }
        if(!good)return 0;
    }
    return 1;
}
int css_next(const char *base,char *url,unsigned cap,unsigned blocked){
    pending=0;
    if(blocked)return 0;
    while(next){
        sheet *s=next;next=s->next;
        if(!s->ref.n||s->ref.n>=HTTP_URL_MAX)continue;
        char ref[HTTP_URL_MAX];
        if(html_resource_url(ref,sizeof(ref),(const uint8_t*)s->ref.p,s->ref.n)&&!http_resolve_url(base,ref,url,cap)&&allowed(base,url,0,0)){pending=s;return 1;}
    }
    return 0;
}
void css_receive(uint8_t *body,unsigned length,unsigned pages,const char *final_url){
    if(pending&&length){
        pending->text=span(body,length);pending->pages=pages;fetched++;
        unsigned n=0;while(final_url[n])n++;
        char *b=alloc(n+1);
        if(b){memcpy(b,final_url,n+1);pending->base=b;}
    }
    else if(pages)pmm_free_pages((uint32_t)body,pages);
}
unsigned css_downloads(void){return fetched;}
const css_style *css_node(unsigned node){
    struct marks *m=maps;
    while(m&&node>=NODE_GROUP){m=m->next;node-=NODE_GROUP;}
    return m&&m->items[node]?&m->items[node]->style:&base_style;
}
int css_dom_range(const uint8_t *id,unsigned n,unsigned *from,unsigned *to){
    if(!n)return 0;
    for(el *e=first;e;e=e->next)if(e->to&&eq(e->id,span(id,n),0)){
        if(from)*from=e->from;
        if(to)*to=e->to;
        return 1;
    }
    return 0;
}

static int hex(char c){
    if(c>='0'&&c<='9')return c-'0';
    c=lower(c);return c>='a'&&c<='f'?c-'a'+10:-1;
}
static int number(str s,int max){
    int v=0;if(!s.n)return -1;
    for(unsigned i=0;i<s.n;i++){
        char c=s.p[i];if(c<'0'||c>'9')return -1;
        v=v*10+c-'0';if(v>max)return -1;
    }
    return v;
}
/* Hundredths; store the relative unit separately until parent sizes exist. */
static unsigned length_value(str s){
    unsigned unit=0;
    if(s.n>10&&lit(sub(s,0,5),"calc(")&&s.p[s.n-1]==')'){
        /* Common responsive root-size idiom: number / number * <vw length>. */
        str v=trim(sub(s,5,s.n-1));
        unsigned div=0,mul=0;
        while(div<v.n&&v.p[div]!='/')div++;
        while(mul<v.n&&v.p[mul]!='*')mul++;
        if(div>=mul||mul>=v.n)return CSS_UNSET;
        int num=number(trim(sub(v,0,div)),65535),den=number(trim(sub(v,div+1,mul)),65535);
        str tail_value=trim(sub(v,mul+1,v.n));
        if(num<0||den<=0||tail_value.n<3||!lit(sub(tail_value,tail_value.n-2,tail_value.n),"vw"))return CSS_UNSET;
        int vws=number(sub(tail_value,0,tail_value.n-2),65535);if(vws<0)return CSS_UNSET;
        uint64_t scaled=(uint64_t)num*vws*100/(unsigned)den;
        return scaled<=0xffffff ? (4u<<24)|(unsigned)scaled : CSS_UNSET;
    }
    if(s.n>3&&lit(sub(s,s.n-3,s.n),"rem")){s.n-=3;unit=2;}
    else if(s.n>2&&lit(sub(s,s.n-2,s.n),"em")){s.n-=2;unit=1;}
    else if(s.n>2&&lit(sub(s,s.n-2,s.n),"px"))s.n-=2;
    else if(s.n>2&&lit(sub(s,s.n-2,s.n),"vw")){s.n-=2;unit=4;}
    else if(s.n>2&&lit(sub(s,s.n-2,s.n),"vh")){s.n-=2;unit=5;}
    else if(s.n>1&&s.p[s.n-1]=='%'){s.n--;unit=3;}
    else if(lit(s,"0"))return 0;
    else return CSS_UNSET;
    unsigned whole=0,fraction=0,places=0;int dot=0,digits=0;
    for(unsigned i=0;i<s.n;i++){
        char c=s.p[i];
        if(c=='.'&&!dot){dot=1;continue;}
        if(c<'0'||c>'9')return CSS_UNSET;
        digits=1;
        if(dot){if(places<2){fraction=fraction*10+(c-'0');places++;}}
        else {whole=whole*10+(c-'0');if(whole>65535)return CSS_UNSET;}
    }
    if(!digits)return CSS_UNSET;
    while(places<2){fraction*=10;places++;}
    unsigned v=whole*100+fraction;
    return unit<<24|v;
}
static unsigned pixels(unsigned value,unsigned relative,unsigned root,unsigned width,unsigned height){
    unsigned unit=value>>24, v=value&0xffffff;
    unsigned base=unit==1||unit==3?relative:unit==2?root:unit==4?width:unit==5?height:1;
    unsigned divisor=unit>=3?10000:100;
    uint64_t px=((uint64_t)base*v+divisor/2)/divisor;
    return px>65535?65535:(unsigned)px;
}
static int media_one(str m,unsigned width){
    m=trim(m);
    if(!m.n)return 1;
    if(m.p[0]!='('){
        str type=word(&m);
        if(!lit(type,"screen")&&!lit(type,"all"))return 0;
        if(!m.n)return 1;
        if(!lit(word(&m),"and"))return 0;
    }
    for(;;){
        m=trim(m);if(!m.n||m.p[0]!='(')return 0;
        unsigned e=1,a=1;while(e<m.n&&m.p[e]!=')')e++;
        while(a<e&&m.p[a]!=':')a++;
        if(e==m.n||a==e)return 0;
        str prop=trim(sub(m,1,a)),val=trim(sub(m,a+1,e));
        if(val.n<3||!lit(sub(val,val.n-2,val.n),"px"))return 0;
        int n=number(sub(val,0,val.n-2),65535);if(n<0)return 0;
        if(lit(prop,"min-width")){if(width<(unsigned)n)return 0;}
        else if(lit(prop,"max-width")){if(width>(unsigned)n)return 0;}
        else return 0;
        m=trim(sub(m,e+1,m.n));
        if(!m.n)return 1;
        if(!lit(word(&m),"and"))return 0;
    }
}
static int media_matches(str m,unsigned width){
    m=trim(m);if(!m.n)return 1;
    for(unsigned a=0;a<m.n;){
        unsigned b=a;while(b<m.n&&m.p[b]!=',')b++;
        if(media_one(sub(m,a,b),width))return 1;
        a=b+1;
    }
    return 0;
}
static unsigned color(str s){
    if(s.n&&(s.p[0]=='#')&&(s.n==4||s.n==7)){
        unsigned rgb=0;
        for(unsigned i=1;i<s.n;i++){
            int h=hex(s.p[i]);if(h<0)return CSS_UNSET;
            rgb=s.n==4?(rgb<<8)|h*17:(rgb<<4)|h;
        }
        return rgb;
    }
    const char *names[]={"black","white","red","green","blue","gray","grey","navy","yellow","purple","orange"};
    const unsigned rgb[]={0,0xffffff,0xff0000,0x008000,0x0000ff,0x808080,0x808080,0x000080,0xffff00,0x800080,0xffa500};
    for(unsigned i=0;i<sizeof(rgb)/sizeof(*rgb);i++)if(lit(s,names[i]))return rgb[i];
    if(s.n>=9&&lit(sub(s,0,4),"rgb(")&&s.p[s.n-1]==')'){
        unsigned rgb=0,j=4;
        for(int i=0;i<3;i++){
            unsigned start=j;
            while(j<s.n-1&&s.p[j]!=',')j++;
            if((j==s.n-1)!=(i==2))return CSS_UNSET;
            int n=number(trim(sub(s,start,j)),255);if(n<0)return CSS_UNSET;
            rgb=(rgb<<8)|n;j++;
        }
        return rgb;
    }
    return CSS_UNSET;
}

typedef struct {unsigned value[17],mask,important;str background;} decl;
/* A declaration list cannot accidentally interpret a semicolon inside a
 * string/function as a new display or color declaration. */
static unsigned token_end(str s,unsigned from,const char *stops){
    char quote=0;unsigned par=0;
    for(unsigned i=from;i<s.n;i++){
        char c=s.p[i];
        if(c=='\\'){if(i+1<s.n)i++;continue;}
        if(quote){if(c==quote)quote=0;continue;}
        if(c=='"'||c=='\''){quote=c;continue;}
        if(c=='('||c=='['){par++;continue;}
        if(c==')'||c==']'){if(par)par--;continue;}
        if(!par)for(const char *q=stops;*q;q++)if(c==*q)return i;
    }
    return s.n;
}
static str image_value(str v){
    if(v.n<6||!lit(sub(v,0,4),"url("))return (str){0};
    unsigned end=4;char quote=0;
    for(;end<v.n;end++){
        char c=v.p[end];if(!c||c=='\\')return (str){0};
        if(quote){if(c==quote)quote=0;continue;}
        if(c=='"'||c=='\'')quote=c;
        else if(c==')')break;
    }
    if(end==v.n)return (str){0};
    str u=trim(sub(v,4,end));
    if(u.n>=2&&(u.p[0]=='"'||u.p[0]=='\'')&&u.p[0]==u.p[u.n-1])u=sub(u,1,u.n-1);
    return u;
}
static void declared(decl *d,unsigned k,unsigned v,int important){
    unsigned bit=1u<<k;
    if(important||!(d->important&bit)){
        d->value[k]=v;d->mask|=bit;
        if(important)d->important|=bit;
    }
}
static decl declarations(str s){
    decl d={0};unsigned i=0;
    while(i<s.n){
        unsigned end=token_end(s,i,";"),colon=token_end(s,i,":;");
        if(colon<end){
            str key=trim(sub(s,i,colon)),v=trim(sub(s,colon+1,end));
            unsigned bang=token_end(v,0,"!");
            int imp=bang<v.n&&lit(trim(sub(v,bang+1,v.n)),"important");
            if(imp)v=trim(sub(v,0,bang));
            unsigned val=CSS_UNSET,k=0;
            if(lit(key,"color"))val=color(v);
            else if(lit(key,"background-color")||lit(key,"background")){
                k=1;val=color(v);if(lit(v,"transparent"))val=0x1000000;
                if(lit(key,"background")){
                    str url=image_value(v);
                    if(url.n||lit(v,"none")||val!=CSS_UNSET){
                        if(imp||!(d.important&(1u<<9)))d.background=url;
                        declared(&d,9,0,imp);
                        if(val==CSS_UNSET)val=0x1000000;
                        unsigned slash=token_end(v,0,"/");
                        str size=slash<v.n?trim(sub(v,slash+1,v.n)):(str){0};
                        /* A single layer; subsequent layers are not painted. */
                        if(size.n)size=sub(size,0,token_end(size,0,","));
                        unsigned fit=lit(trim(size),"cover")?2:lit(trim(size),"contain")?1:
                                     lit(trim(size),"100%")||lit(trim(size),"100% auto")?3:
                                     lit(trim(size),"100% 100%")?4:0;
                        declared(&d,10,fit,imp);
                        str before=slash<v.n?sub(v,0,slash):v;
                        declared(&d,11,has_word(before,span("center",6),1),imp);
                    }
                }
            } else if(lit(key,"display")){
                k=2;if(lit(v,"none"))val=1;
                else if(lit(v,"flex")||lit(v,"inline-flex"))val=2;
                else if(lit(v,"block"))val=3;
                else if(lit(v,"inline")||lit(v,"inline-block")||
                        lit(v,"grid")||lit(v,"table")||lit(v,"list-item"))val=0;
            } else if(lit(key,"text-align")){
                k=3;if(lit(v,"center"))val=1;else if(lit(v,"right")||lit(v,"end"))val=2;
                else if(lit(v,"left")||lit(v,"start"))val=0;
            } else if(lit(key,"margin-top")||lit(key,"padding-top")||lit(key,"font-size")){
                k=lit(key,"font-size")?5:4;
                if(k==5)val=length_value(v);
                else if(v.n>=3&&lit(sub(v,v.n-2,v.n),"px")){
                    int n=number(sub(v,0,v.n-2),65535);if(n>=0)val=(unsigned)n;
                } else if(lit(v,"0"))val=0;
            } else if(lit(key,"width")||lit(key,"height")||lit(key,"min-height")){
                k=lit(key,"width")?6:lit(key,"height")?7:8;
                val=lit(v,"auto")?0:length_value(v);
            } else if(lit(key,"background-image")){
                k=9;str url=image_value(v);
                if(url.n||lit(v,"none")){val=0;if(imp||!(d.important&(1u<<k)))d.background=url;}
            } else if(lit(key,"background-size")){
                k=10;
                if(lit(v,"contain"))val=1;else if(lit(v,"cover"))val=2;
                else if(lit(v,"100%")||lit(v,"100% auto"))val=3;
                else if(lit(v,"100% 100%"))val=4;else if(lit(v,"auto"))val=0;
            } else if(lit(key,"background-position")){
                k=11;val=has_word(v,span("center",6),1);
            } else if(lit(key,"margin")){
                k=12;str a=word(&v),b=word(&v);val=lit(b.n?b:a,"auto");
            } else if(lit(key,"flex-direction")){
                k=13;
                if(lit(v,"row"))val=0;
                else if(lit(v,"column"))val=1;
            } else if(lit(key,"gap")||lit(key,"column-gap")){
                k=14;str a=word(&v),b=word(&v);
                val=lit(a,"normal")?0:length_value(b.n?b:a);
            } else if(lit(key,"visibility")){
                k=15;
                if(lit(v,"hidden")||lit(v,"collapse"))val=1;
                else if(lit(v,"visible"))val=0;
            } else if(lit(key,"opacity")){
                k=16;
                if(lit(v,"0"))val=1;
                else if(lit(v,"1"))val=0;
            }
            if(val!=CSS_UNSET)declared(&d,k,val,imp);
        }
        i=end+1;
    }
    return d;
}
static void cascade(el *e,const decl *d,unsigned weight,const char *base){
    unsigned v[17]={e->style.fg,e->style.bg,e->display,e->style.align,e->style.gap,e->style.size,
                    e->w,e->h,e->min_h,0,e->fit,e->center,e->center_box,e->column,e->spacing,e->visibility,
                    (e->visibility>>1)&1};
    v[15]&=1;
    for(unsigned k=0;k<17;k++)if(d->mask&(1u<<k)){
        unsigned score=weight+((d->important&(1u<<k))?0x80000000u:0);
        if(score>=e->score[k]){
            e->score[k]=score;v[k]=d->value[k];
            if(k==9){e->background=d->background;e->background_base=base;}
        }
    }
    e->style=(css_style){v[0],v[1],v[2]==1,v[3],v[4],v[5]};
    e->w=v[6];e->h=v[7];e->min_h=v[8];e->fit=v[10];e->center=v[11]; e->center_box=v[12];
    e->display=v[2];e->column=v[13];e->spacing=v[14];e->visibility=v[15]|v[16]<<1;
}
static int compound(el *e,str s){
    unsigned i=0;
    if(s.n&&s.p[0]=='*')i++;
    else {
        while(i<s.n&&ident(s.p[i]))i++;
        if(i&&!eq(e->tag,sub(s,0,i),1))return 0;
    }
    if(!s.n)return 0;
    while(i<s.n){
        char c=s.p[i++];unsigned a=i;while(i<s.n&&ident(s.p[i]))i++;
        str token=sub(s,a,i);
        if(!token.n)return 0;
        if(c=='.'){if(!has_word(e->classes,token,0))return 0;}
        else if(c=='#'){if(!eq(e->id,token,0))return 0;}
        else if(c==':'){if(!lit(token,"root")||e->parent)return 0;}
        else return 0;
    }
    return 1;
}
/* Walk the selector from the subject to its ancestors. For a descendant
 * chain, a nearer matching ancestor dominates farther candidates. */
static int matches(el *e,str selector){
    int direct=1;
    for(;;){
        selector=trim(selector);
        unsigned end=selector.n,at=end;
        while(at&&!ws(selector.p[at-1])&&selector.p[at-1]!='>')at--;
        str c=sub(selector,at,end);
        while(e&&!compound(e,c)){if(direct)return 0;e=e->parent;}
        if(!e)return 0;
        selector=trim(sub(selector,0,at));
        if(!selector.n)return 1;
        direct=selector.p[selector.n-1]=='>';
        if(direct)selector.n--;
        e=e->parent;
    }
}
static unsigned specificity(str s){
    unsigned ids=0,classes=0,tags=0;int start=1;
    for(unsigned i=0;i<s.n;i++){
        char c=s.p[i];
        if(c=='#'){if(ids<255)ids++;start=0;}
        else if(c=='.'||c==':'){if(classes<255)classes++;start=0;}
        else if(ws(c)||c=='>')start=1;
        else if(start){if(ident(c)&&tags<255)tags++;start=0;}
    }
    return (ids<<16)|(classes<<8)|tags;
}
static int block_end(str s,unsigned open){
    unsigned depth=1,off=open+1;
    while(off<s.n){
        unsigned j=token_end(s,off,"{}");
        if(j==s.n)break;
        if(s.p[j]=='{')depth++;else if(!--depth)return (int)j;
        off=j+1;
    }
    return -1;
}
static void apply_sheet(str s,unsigned width,int media_allowed,const char *base){
    unsigned i=0;
    while(i<s.n){
        unsigned open=token_end(s,i,"{;");
        if(open==s.n)break;
        if(s.p[open]==';'){i=open+1;continue;}
        int close=block_end(s,open);if(close<0)break;
        str selectors=trim(sub(s,i,open));i=(unsigned)close+1;
        if(!selectors.n)continue;
        if(selectors.p[0]=='@'){
            if(media_allowed&&selectors.n>6&&lit(sub(selectors,0,6),"@media")&&
               media_matches(sub(selectors,6,selectors.n),width))
                apply_sheet(sub(s,open+1,(unsigned)close),width,0,base);
            continue;
        }
        decl d=declarations(sub(s,open+1,(unsigned)close));if(!d.mask)continue;
        unsigned a=0;
        while(a<selectors.n){
            unsigned b=token_end(selectors,a,",");
            str sel=trim(sub(selectors,a,b));a=b+1;
            if(!sel.n)continue;
            unsigned weight=specificity(sel);
            for(el *e=first;e;e=e->next)if(matches(e,sel))cascade(e,&d,weight,base);
        }
    }
}
/* Tokenization keeps quoted and escaped comment markers literal. */
static void clean_and_apply(sheet *sh,unsigned width){
    str s=sh->text;if(!s.n)return;
    if(sh->clean){apply_sheet(s,width,1,sh->base?sh->base:doc_url);return;}
    unsigned pages=sh->pages?sh->pages:(s.n+4095)/4096;
    char *buf=sh->pages?(void*)s.p:(void*)pmm_alloc_pages(pages);if(!buf)return;
    unsigned n=0;char q=0;
    for(unsigned i=0;i<s.n;i++){
        char c=s.p[i];
        if(c=='\\'&&i+1<s.n){char next=s.p[++i];buf[n++]=c;buf[n++]=next;continue;}
        if(q){if(c==q)q=0;}
        else if(c=='"'||c=='\'')q=c;
        else if(c=='/'&&i+1<s.n&&s.p[i+1]=='*'){
            i+=2;while(i+1<s.n&&(s.p[i]!='*'||s.p[i+1]!='/'))i++;
            if(i+1==s.n)break;
            i++;buf[n++]=' ';continue;
        }
        buf[n++]=c;
    }
    sh->pages=pages;sh->text=span(buf,n);sh->clean=1;
    apply_sheet(sh->text,width,1,sh->base?sh->base:doc_url);
}
void css_resolve(unsigned blocked,unsigned width,unsigned height){
    for(el *e=first;e;e=e->next){
        e->style=base_style;memset(e->score,0,sizeof(e->score));
        e->w=e->h=e->min_h=e->fit=e->center=e->center_box=e->display=e->column=e->spacing=e->visibility=0;
        e->background=(str){0};e->background_base=0;e->image=0;
    }
    if(!blocked){
        /* Replay in DOM order, not in response-completion order. */
        int inline_ok=allowed(0,0,1,0);
        for(sheet *s=sheets;s;s=s->next)if((s->ref.n||inline_ok)&&media_matches(s->media,width))clean_and_apply(s,width);
        if(allowed(0,0,1,1))for(el *e=first;e;e=e->next)if(e->inline_style.n){
            decl d=declarations(e->inline_style);cascade(e,&d,0x1000000,doc_url);
        }
    }
    root_size=16;
    for(el *e=first;e;e=e->next){
        el *p=e->parent;
        e->paint_bg=e->style.bg;
        if(e->visibility)e->style.hidden=1;
        if(e->score[5]){
            unsigned px=pixels(e->style.size,p&&p->style.size?p->style.size:16,root_size,width,height);
            e->style.size=px?px:1;
        }
        if(!p){if(lit(e->tag,"html")&&e->style.size)root_size=e->style.size;continue;}
        if(p->style.hidden)e->style.hidden=1;
        if(!e->score[0]&&e->style.fg==CSS_UNSET)e->style.fg=p->style.fg;
        if(e->style.bg==CSS_UNSET||e->style.bg==0x1000000)e->style.bg=p->style.bg;
        if(!e->score[3]&&!e->style.align)e->style.align=p->style.align;
        if(!e->score[5]&&!e->style.size)e->style.size=p->style.size;
    }
}
uint32_t css_canvas(void){
    for(el *e=first;e;e=e->next)if(lit(e->tag,"body")&&e->style.bg<CSS_UNSET)return e->style.bg;
    return CSS_UNSET;
}
static int block_box(const el *e){
    const char *tags[]={"div","section","main","article","aside","header","footer","nav","p",
                         "figure","li","ul","ol","table","form","pre","body"};
    for(unsigned i=0;i<sizeof(tags)/sizeof(*tags);i++)if(lit(e->tag,tags[i]))return 1;
    return 0;
}
static int row_flex(const el *e){return e&&e->display==2&&!e->column;}
static int needs_box(const el *e){
    if(e->display>=2||(e->parent&&e->parent->display==2))return 1;
    return block_box(e)&&(e->w||e->h||e->min_h||e->background.n||e->paint_bg<=0xffffff);
}
static struct marks *mark_group(unsigned *i){
    struct marks *m=maps;
    while(m&&*i>=NODE_GROUP){m=m->next;*i-=NODE_GROUP;}
    return m;
}
void css_flow_reset(int width,int height){
    view_width=width;view_height=height;flow=0;
    for(struct marks *m=maps;m;m=m->next)memset(m->starts,0,sizeof(m->starts));
    for(el *e=last;e;e=e->prev){
        e->used=0;
        if(!e->to || e->style.hidden || !needs_box(e))continue;
        unsigned i=e->from;struct marks *m=mark_group(&i);if(!m)continue;
        e->opening=m->starts[i];m->starts[i]=e;
    }
}
static unsigned element_width(el *e,unsigned available){
    unsigned font=e->style.size?e->style.size:16;
    unsigned w=pixels(e->w,(e->w>>24)==3?available:font,root_size,view_width,view_height);
    return w>available?available:w;
}
static void prepare_row(el *e){
    unsigned count=0,auto_count=0,total=0,available=(unsigned)e->box_w;
    for(el *k=e->child;k;k=k->sibling)if(k->to&&!k->style.hidden){
        unsigned w=element_width(k,available);
        if(w)total+=w;else auto_count++;
        count++;
    }
    unsigned font=e->style.size?e->style.size:16;
    unsigned gap=pixels(e->spacing,(e->spacing>>24)==3?available:font,root_size,view_width,view_height);
    if(count>1&&gap>available/(count-1))gap=available/(count-1);
    unsigned space=available-(count>1?(count-1)*gap:0);
    unsigned fixed=total+auto_count;
    e->items_x=0;e->items_h=0;e->item_gap=(int)gap;
    e->row_space=(int)space;
    e->shrink_sum=fixed>space?(int)fixed:0;
    e->auto_width=auto_count&&total<space?(int)((space-total)/auto_count):1;
    e->share_more=auto_count&&total<space?(int)((space-total)%auto_count):0;
}
int css_flow_step(unsigned node,int y,int *left,int *width){
    while(flow&&node>=flow->to){
        if(y<flow->y+flow->box_h)y=flow->y+flow->box_h;
        if(y>flow->y)flow->box_h=y-flow->y;
        el *done=flow;
        flow=flow->outer;
        if(row_flex(flow)&&done->parent==flow){
            if(done->box_h>flow->items_h)flow->items_h=done->box_h;
            y=flow->y+flow->items_h;
        }
    }
    unsigned i=node;struct marks *m=mark_group(&i);
    for(el *e=m?m->starts[i]:0;e;e=e->opening){
        int available=flow?flow->box_w:view_width;
        e->outer=flow;e->used=1;
        e->x=flow?flow->x:0;
        int item=row_flex(flow)&&e->parent==flow;
        if(item){y=flow->y;e->x+=flow->items_x;}
        e->y=y;
        unsigned font=e->style.size?e->style.size:16;
        unsigned w=element_width(e,(unsigned)available);
        unsigned parent_height=flow&&flow->box_h>0?(unsigned)flow->box_h:(unsigned)view_height;
        unsigned h=pixels(e->h,(e->h>>24)==3?parent_height:font,root_size,view_width,view_height);
        unsigned min=pixels(e->min_h,(e->min_h>>24)==3?parent_height:font,root_size,view_width,view_height);
        if(item){
            if(flow->shrink_sum)w=(w?w:1)*(unsigned)flow->row_space/(unsigned)flow->shrink_sum;
            else if(!w){w=(unsigned)flow->auto_width;if(flow->share_more){w++;flow->share_more--;}}
            if(!w)w=1;
        }
        e->box_w=w?(int)w:available;
        if(!item&&e->center_box)e->x+=(available-e->box_w)/2;
        e->box_h=(int)(min>h?min:h);
        if(item)flow->items_x+=e->box_w+flow->item_gap;
        if(row_flex(e))prepare_row(e);
        flow=e;
    }
    *left=flow?flow->x:0;*width=flow?flow->box_w:view_width;
    return y;
}
int css_flow_decorated(void){
    for(el *e=flow;e;e=e->outer)if(e->background.n)return 1;
    return 0;
}
static asset *background_for(el *e){
    if(e->image)return e->image;
    if(!e->background.n||e->background.n>=HTTP_URL_MAX||!e->background_base)return 0;
    char ref[HTTP_URL_MAX],url[HTTP_URL_MAX];unsigned n=e->background.n;
    /* URL-valued styles never interpret escapes or embedded control bytes. */
    for(unsigned i=0;i<n;i++){char c=e->background.p[i];if((unsigned char)c<=32||c==127||c=='\\')return 0;ref[i]=c;}
    ref[n]=0;
    if(ref[0]=='#'||http_resolve_url(e->background_base,ref,url,sizeof(url)))return 0;
    n=0;while(url[n])n++;
    for(asset *a=assets;a;a=a->next)if(eq(span(url,n),span(a->url,a->length),0))return e->image=a;
    asset *a=alloc(sizeof(*a));char *save=alloc(n+1);
    if(!a||!save)return 0;
    memcpy(save,url,n+1);a->url=save;a->length=n;a->next=assets;assets=a;return e->image=a;
}
int css_next_background(char *url,unsigned cap,int top,int height){
    for(el *e=first;e;e=e->next){
        if(!e->used||!e->background.n||e->box_h<=0||e->y+e->box_h<=top-height||e->y>=top+2*height)continue;
        asset *a=background_for(e);if(!a||a->requested||a->length>=cap)continue;
        a->requested=1;a->attempts++;background_pending=a;memcpy(url,a->url,a->length+1);
        return 1;
    }
    return 0;
}
void css_background_received(web_bitmap *bmp,int retry){
    if(background_pending){
        if(bmp->rgb){background_pending->bmp=*bmp;*bmp=(web_bitmap){0};}
        else if(retry&&background_pending->attempts==1)background_pending->requested=0;
    }
    background_pending=0;
}
unsigned css_backgrounds(void){
    unsigned n=0;for(asset *a=assets;a;a=a->next)n+=a->bmp.rgb!=0;return n;
}
void css_paint_boxes(int x,int y,int scroll){
    int cx,cy,cw,ch;gfx_get_clip(&cx,&cy,&cw,&ch);
    for(el *e=first;e;e=e->next){
        if(!e->used||e->box_h<=0||e->y+e->box_h<=scroll||e->y>=scroll+view_height)continue;
        int bx=e->x+x,by=e->y+y-scroll,bw=e->box_w,bh=e->box_h;
        if(e->paint_bg<=0xffffff)gfx_rgb_fill(bx,by,bw,bh,e->paint_bg);
        if(!e->image||!e->image->bmp.rgb||bw<=0)continue;
        const web_bitmap *b=&e->image->bmp;
        int w=(int)b->w,h=(int)b->h;
        if(e->fit==4){w=bw;h=bh;}
        else if(e->fit){
            int wide=((uint32_t)bw*b->h)<((uint32_t)bh*b->w);
            if(e->fit==3||(e->fit==1&&wide)||(e->fit==2&&!wide)){w=bw;h=(bw*b->h+b->w-1)/b->w;}
            else {h=bh;w=(bh*b->w+b->h-1)/b->h;}
        }
        int l=bx>cx?bx:cx,t=by>cy?by:cy,r=bx+bw<cx+cw?bx+bw:cx+cw,d=by+bh<cy+ch?by+bh:cy+ch;
        if(r<=l||d<=t)continue;
        gfx_set_clip(l,t,r-l,d-t);
        gfx_bitmap565(bx+(e->center?(bw-w)/2:0),by+(e->center?(bh-h)/2:0),w,h,b->rgb,b->w,b->h);
        gfx_set_clip(cx,cy,cw,ch);
    }
}
