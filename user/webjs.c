/* Ring-3 web script interpreter. Only native IPC hooks are exposed. */
#include "quickjs.h"
#include <stdint.h>
#include <stdlib.h>

extern int read(int,void *,unsigned);
extern int write(int,const void *,unsigned);
static int dom_read(const char *id,size_t n,char *out){
    int result;
    __asm__ volatile("int $0x80":"=a"(result):"0"(107),"b"(id),"c"(n),"d"(out):"memory");
    return result;
}
static JSValue dom_get(JSContext *ctx,JSValueConst self,int argc,JSValueConst *argv){
    (void)self;
    if(!argc)return JS_NULL;
    size_t n;const char *id=JS_ToCStringLen(ctx,&n,argv[0]);
    if(!id)return JS_EXCEPTION;
    int len=dom_read(id,n,0);
    JSValue result=JS_NULL;
    if(len==0)result=JS_NewStringLen(ctx,"",0);
    if(len>0){
        char *buf=malloc((unsigned)len);
        if(!buf)result=JS_ThrowOutOfMemory(ctx);
        else {
            if(dom_read(id,n,buf)==len)result=JS_NewStringLen(ctx,buf,(unsigned)len);
            free(buf);
        }
    }
    JS_FreeCString(ctx,id);return result;
}
static JSValue dom_put(JSContext *ctx,JSValueConst self,int argc,JSValueConst *argv){
    (void)self;
    if(argc!=2)return JS_UNDEFINED;
    size_t n,m;const char *id=JS_ToCStringLen(ctx,&n,argv[0]);
    if(!id)return JS_EXCEPTION;
    const char *text=JS_ToCStringLen(ctx,&m,argv[1]);
    if(!text){JS_FreeCString(ctx,id);return JS_EXCEPTION;}
    uint16_t hdr[2]={(uint16_t)n,(uint16_t)m};
    int ok=n<=65535&&m<=65535&&write(1,hdr,4)==4&&
           write(1,id,(unsigned)n)==(int)n&&(!m||write(1,text,(unsigned)m)==(int)m);
    JS_FreeCString(ctx,text);JS_FreeCString(ctx,id);
    if(!ok)return JS_ThrowRangeError(ctx,"document output does not fit");
    return JS_UNDEFINED;
}
/* A capability bridge, not an emulated HTML engine: update text only.
 * Callbacks run after all supported classic scripts, in registration order. */
static const char bootstrap[]=
"(() => { const read=globalThis.__ceruliaNativeGet, put=globalThis.__ceruliaNativePut;"
"delete globalThis.__ceruliaNativeGet; delete globalThis.__ceruliaNativePut;"
"const nodes=new Map(), ready=[]; let phase='loading';"
"function get(id){ id=String(id); if(nodes.has(id))return nodes.get(id);"
"let text=read(id); if(text===null)return null; const el={};"
"Object.defineProperties(el,{id:{value:id},textContent:{get(){return text;},"
"set(v){const s=v==null?'':String(v);put(id,s);text=s;}}});nodes.set(id,el);return el;}"
"const doc={getElementById:get,querySelector(s){s=String(s);return s[0]==='#'?get(s.slice(1)):null;},"
"get readyState(){return phase;},addEventListener(t,f){"
"if(t==='DOMContentLoaded'&&typeof f==='function'&&phase==='loading')ready.push(f);}};"
"globalThis.window=globalThis;globalThis.document=doc;"
"return ()=>{phase='interactive';for(const f of ready)f.call(doc,{type:'DOMContentLoaded',target:doc});"
"phase='complete';};})()";
static int poll_run(JSRuntime *rt,void *opaque){
    int r;(void)rt;(void)opaque;
    __asm__ volatile("int $0x80":"=a"(r):"0"(105):"memory");
    return r!=0;
}
static int input(void *dst,unsigned n){
    unsigned pos=0;
    while(pos<n){
        int got=read(0,(char*)dst+pos,n-pos);
        if(got<=0)return 0;
        pos+=(unsigned)got;
    }
    return 1;
}
int umain(void){
    unsigned short control;
    __asm__ volatile("fnstcw %0":"=m"(control));
    control=(control&~0x300u)|0x200u;
    __asm__ volatile("fldcw %0"::"m"(control));
    JSRuntime *rt=JS_NewRuntime();
    if(!rt)return 3;
    JS_SetMaxStackSize(rt,64*1024);
    JS_SetMemoryLimit(rt,2*1024*1024);
    JS_SetInterruptHandler(rt,poll_run,0);
    JSContext *ctx=JS_NewContext(rt);
    if(!ctx)return 3;
    JSValue global=JS_GetGlobalObject(ctx);
    JS_SetPropertyStr(ctx,global,"__ceruliaNativeGet",JS_NewCFunction(ctx,dom_get,"readText",1));
    JS_SetPropertyStr(ctx,global,"__ceruliaNativePut",JS_NewCFunction(ctx,dom_put,"put",2));
    JS_FreeValue(ctx,global);
    JSValue ready=JS_Eval(ctx,bootstrap,sizeof(bootstrap)-1,"native-dom",JS_EVAL_TYPE_GLOBAL);
    if(JS_IsException(ready))return 3;
    for(;;){
        uint32_t n;
        if(!input(&n,sizeof(n)))return 2;
        if(!n)break;
        if(n>0x100000)return 2;
        char *src=malloc(n+1);
        if(!src||!input(src,n))return 2;
        src[n]=0;
        JSValue val=JS_Eval(ctx,src,n,"page.js",JS_EVAL_TYPE_GLOBAL);
        free(src);
        if(JS_IsException(val))return 4;
        JS_FreeValue(ctx,val);
    }
    JSValue result=JS_Call(ctx,ready,JS_UNDEFINED,0,0);
    JS_FreeValue(ctx,ready);
    if(JS_IsException(result))return 4;
    JS_FreeValue(ctx,result);
    JSContext *jobctx;
    int status;
    while((status=JS_ExecutePendingJob(rt,&jobctx))>0){}
    if(status<0)return 4;
    JS_FreeContext(ctx);JS_FreeRuntime(rt);
    return 0;
}
