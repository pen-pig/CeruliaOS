/* Narrow report-mode keyboard support. Recognize the standard boot-shaped
 * eight-byte input layout, with an optional single report ID. Do not infer
 * layout from endpoint size. Report-mode output formats are not assumed. */
#ifndef CERULIA_HID_KEY_LAYOUT_H
#define CERULIA_HID_KEY_LAYOUT_H

#define HID_KEY_NO_ID 256u

/* Report-mode NKRO: one Keyboard application and one contiguous bitmap
 * of keyboard-page usages. Allow padding and a separate modifier byte.
 * Do not treat unrelated input fields, report IDs or vendor usages as keys. */
static hid_key_bitmap_t hid_key_bitmap(const uint8_t *src,unsigned n) {
    hid_key_bitmap_t out={.mod_bit=0xffff};
    unsigned page=0,size=0,count=0,depth=0,app=0,bits=0,keys=0;
    unsigned first=256,last=256,usage=0,min=0,max=0;
    for(unsigned i=0;i<n;){
        unsigned k=src[i++],len=k&3u,v=0;
        if(len==3)len=4;
        if(k==254 || len>n-i)return (hid_key_bitmap_t){0};
        for(unsigned j=0;j<len;j++)v|=(unsigned)src[i++]<<(8*j);
        switch(k&252u){
        case 0x04:page=v;break;
        case 0x14:min=v;break;
        case 0x24:max=v;break;
        case 0x74:size=v;break;
        case 0x94:count=v;break;
        case 0x84:
            if(!v || v>255 || (out.id && out.id!=v) || (bits && !out.id))
                return (hid_key_bitmap_t){0};
            out.id=(uint8_t)v;
            break;
        case 0x08:usage=v;break;
        case 0x18:first=v;break;
        case 0x28:last=v;break;
        case 0xa0:
            if(!depth && (app++ || page!=1 || usage!=6 || v!=1))
                return (hid_key_bitmap_t){0};
            if(++depth>8)return (hid_key_bitmap_t){0};
            goto clear;
        case 0xc0:
            if(!depth)return (hid_key_bitmap_t){0};
            depth--;
            goto clear;
        case 0x80:
            if(!depth || !size || !count || size>64 || count>512 || size*count>512u-bits)
                return (hid_key_bitmap_t){0};
            if(!(v&1u)){
                if(v!=2 || page!=7 || size!=1 || min || max!=1 ||
                   first>last || last>231 || last-first+1u!=count)
                    return (hid_key_bitmap_t){0};
                if(first==224 && last==231 && out.mod_bit==0xffff)
                    out.mod_bit=(uint16_t)bits;
                else if(!keys && first<=4 && last>=29){
                    out.key_bit=(uint16_t)bits;
                    out.first=(uint8_t)first;
                    out.last=(uint8_t)last;
                    keys=1;
                }else return (hid_key_bitmap_t){0};
            }
            bits+=size*count;
            goto clear;
        case 0x90:case 0xb0:
clear:
            first=last=256;usage=0;
            break;
        case 0x34:case 0x44:case 0x54:case 0x64:break;
        default:return (hid_key_bitmap_t){0};
        }
    }
    unsigned bytes=(bits+7u)/8u+!!out.id;
    if(depth || app!=1 || !keys || bytes>64 || (out.last>=224 && out.mod_bit!=0xffff))
        return (hid_key_bitmap_t){0};
    out.bytes=(uint8_t)bytes;
    return out;
}

static unsigned hid_key_layout(const uint8_t *src,unsigned n) {
    unsigned page=0,size=0,count=0,id=0,bit=0,depth=0,member=0;
    unsigned usage=0,first=0,last=0,logical_min=0,logical_max=0;
    for(unsigned i=0;i<n;) {
        unsigned k=src[i++],len=k&3u,v=0;
        if(len==3)len=4;
        if(k==254 || len>n-i)return 0;
        for(unsigned j=0;j<len;j++)v|=(unsigned)src[i++]<<(8*j);
        switch(k&252u) {
        case 0x04:page=v;break;
        case 0x14:logical_min=v;break;
        case 0x24:logical_max=v;break;
        case 0x74:size=v;break;
        case 0x94:count=v;break;
        case 0x84:
            if(!v || v>255 || (id && id!=v) || (bit && !id))return 0;
            id=v;break;
        case 0x08:usage=v;break;
        case 0x18:first=v;break;
        case 0x28:last=v;break;
        case 0xa0:
            if(!depth && (member++ || page!=1 || usage!=6 || v!=1))return 0;
            depth++;usage=first=last=0;break;
        case 0xc0:
            if(!depth)return 0;
            depth--;usage=first=last=0;break;
        case 0x80:
            if(!depth || !size || !count || size>64 || count>64 || size*count>64-bit)return 0;
            if(!bit) {
                if(v!=2 || size!=1 || count!=8 || page!=7 || first!=224 || last!=231 ||
                   logical_min || logical_max!=1)return 0;
            } else if(bit==8) {
                if(!(v&1u) || size*count!=8)return 0;
            } else if(bit!=16 || v!=0 || size!=8 || count!=6 || page!=7 ||
                      first!=0 || last<101 || last>255 || logical_min ||
                      logical_max<101 || logical_max>255) return 0;
            bit+=size*count;
            usage=first=last=0;
            break;
        case 0x90:case 0xb0:usage=first=last=0;break; /* output/feature unused */
        case 0x34:case 0x44:case 0x54:case 0x64:break;
        default:return 0;
        }
    }
    return !depth && member==1 && bit==64 ? (id ? id : HID_KEY_NO_ID) : 0;
}

#endif
