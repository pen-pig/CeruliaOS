#ifndef CERULIA_WEBURL_H
#define CERULIA_WEBURL_H
/* Decode ampersands in HTML resource attributes, not in CSS url() or HTTP
 * Location headers. Resolve and enforce origin policy on the result. */
static unsigned html_resource_url(char *dst,unsigned cap,const uint8_t *src,unsigned n){
    static const char *const amp[]={"amp;","AMP;","#38;","#x26;","#X26;"};
    unsigned used=0;
    for(unsigned i=0;i<n;){
        unsigned c=src[i++];
        if(c<=32||c==127||c==92||used+1>=cap)return 0;
        if(c=='&'){
            for(unsigned k=0;k<sizeof(amp)/sizeof(amp[0]);k++){
                const char *s=amp[k];unsigned j=0;
                while(s[j]&&j<n-i&&src[i+j]==(unsigned char)s[j])j++;
                if(!s[j]){i+=j;break;}
            }
        }
        dst[used++]=(char)c;
    }
    if(cap)dst[used]=0;
    return used;
}
#endif
