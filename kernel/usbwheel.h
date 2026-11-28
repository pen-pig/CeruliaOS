/* Recognize unnumbered relative mouse and 15-bit absolute tablet layouts.
 * A descriptor that doesn't match exactly falls back to Boot HID if available. */
#ifndef USB_WHEEL_LAYOUT_H
#define USB_WHEEL_LAYOUT_H
#define HID_REL_WHEEL 1
#define HID_ABS_WHEEL 2
#define HID_REL16 3
#define HID_REL16_WHEEL 4
static inline unsigned hid_pointer_bytes(unsigned type) {
    return type==HID_REL16?5u : type==HID_REL_WHEEL?4u : 6u;
}
static inline int hid_pointer_layout_id(const uint8_t *p,unsigned n,unsigned *report_id) {
    int absolute=0,wide=0;
    unsigned id=0;
    unsigned page=0,size=0,count=0,bit=0,used=0,usage[3]={0},depth=0;
    unsigned low=0,high=0,buttons=0,lo=0,hi=0;
    for(unsigned i=0;i<n;){
        unsigned k=p[i++],len=k&3u,v=0;
        if(len==3)len=4;
        if(k==254 || len>n-i)return 0;
        for(unsigned j=0;j<len;j++)v|=(unsigned)p[i++]<<(8*j);
        switch(k&252u){
        case 0x04:page=v;break;
        case 0x14:lo=v;break;
        case 0x24:hi=v;break;
        case 0x74:size=v;break;
        case 0x94:count=v;break;
        case 0x84:if(bit || !v || v>255 || (id && id!=v))return 0;id=v;break;
        case 0x08:if(used==3)return 0;usage[used++]=v;break;
        case 0x18:low=v;break;
        case 0x28:high=v;break;
        case 0xa0:
            if(!depth && !(v==1 && page==1 && used==1 && usage[0]==2))return 0;
            depth++;used=low=high=0;break;
        case 0xc0:if(!depth)return 0;depth--;used=low=high=0;break;
        case 0x80:
            if(!depth||!size||!count||count>32||size>32||count*size>48-bit)return 0;
            if(!bit){
                if(page!=9||v!=2||size!=1||count<3||count>8||low!=1||high!=count)return 0;
                buttons=count;
            } else if(bit<8){
                if(!(v&1)||bit+count*size!=8)return 0;
            } else if (bit==8 && v==2 && size==16 && page==1 && count==2 &&
                       used==2 && usage[0]==0x30 && usage[1]==0x31 && lo==0 && hi==32767) {
                absolute=1;
            } else if(bit==8 && v==6 && size==16 && page==1 && count==2 &&
                      used==2 && usage[0]==0x30 && usage[1]==0x31 && hi==32767 &&
                      (lo==0x8000 || lo==0x8001 || lo==0xffff8000 || lo==0xffff8001)) {
                wide=1;
            } else {
                if(v!=6||size!=8||page!=1||used!=count||hi!=127||
                    (lo!=129&&lo!=128&&lo!=0xff81&&lo!=0xff80))return 0;
                if(absolute || wide) {
                    if(bit!=40 || count!=1 || usage[0]!=0x38)return 0;
                } else {
                    const unsigned axes[]={0x30,0x31,0x38};
                    if(bit+count*size>32)return 0;
                    for(unsigned j=0;j<count;j++)if(usage[j]!=axes[(bit-8)/8+j])return 0;
                }
            }
            bit+=count*size;used=low=high=0;break;
        case 0x34:case 0x44:break; /* physical min/max do not affect raw input */
        default:return 0; /* no report IDs, long items, pushed global state etc. */
        }
    }
    if(!buttons||depth)return 0;
    if(report_id)*report_id=id;
    if(wide)return bit==48?HID_REL16_WHEEL:bit==40?HID_REL16:0;
    return absolute ? (bit==48 ? HID_ABS_WHEEL : 0) : (bit==32 ? HID_REL_WHEEL : 0);
}
static inline int hid_pointer_layout(const uint8_t *p,unsigned n){
    return hid_pointer_layout_id(p,n,0);
}
static inline int hid_wheel_layout(const uint8_t *p,unsigned n) {
    return hid_pointer_layout(p,n)==HID_REL_WHEEL;
}
#endif
