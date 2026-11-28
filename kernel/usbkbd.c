/*
 * usbkbd.c - USB HID 键盘：boot 报告解析 + 轮询（真机点亮 H2-2d）
 *
 * 报告格式（USB HID boot interface，键盘协议）：
 *   [0]     修饰键位图：bit0 LCtrl bit1 LShift bit2 LAlt bit3 LGUI
 *                       bit4 RCtrl bit5 RShift bit6 RAlt bit7 RGUI
 *   [1]     保留
 *   [2..7]  键码数组（最多 6 键同时按下，0 = 空位）
 *
 * Compare key sets for edges. Typematic runs from the system clock and
 * does not depend on whether a device resends identical idle reports.
 * Controller input is now nonblocking on UHCI and xHCI.
 *  - 与 PS/2 共用缓冲：注入走 keyboard_inject()，由 keyboard.c 负责关中断
 *    保护，本模块不直接碰缓冲。
 *  - 静默原则：NAK（没有新报告）是每 10ms 发生一次的常态，绝不打印。
 */
#include "usbkbd.h"
#include "usbenum.h"
#include "usbhc.h"
#include "keyboard.h"
#include "isr.h"
#include "irqflags.h"
#include "dmesg.h"
#include "types.h"
#include "user.h"

#define USBKBD_THROTTLE_MS 5u      /* 轮询节流：键盘 interval=10ms */
#define USBKBD_REPORT_LEN  8u      /* boot 协议键盘报告固定 8 字节 */
#define REPEAT_DELAY 500u
#define REPEAT_PERIOD 50u
#define NON_SHIFT_MODS 0xDDu /* Ctrl, Alt and GUI, on either side */

typedef struct {
    const usb_dev_t *dev;
    const usb_hid_if_t *keys;
    uint8_t previous[32], modifiers, repeat, got_report;
    uint32_t repeat_after;
    int led_sent, led_failed;
} kbd_t;
static kbd_t keyboards[USBENUM_MAX_DEV];
static int caps,num;
static uint32_t last_poll;

static void k_str(char *b, int *n, int lim, const char *s) {
    while (*s && *n < lim) b[(*n)++] = *s++;
}

static void k_dec(char *b, int *n, int lim, uint32_t v) {
    char t[12];
    int m = 0;
    if (v == 0) t[m++] = '0';
    while (v) { t[m++] = (char)('0' + v % 10); v /= 10; }
    while (m && *n < lim) b[(*n)++] = t[--m];
}

static void k_hex2(char *b, int *n, int lim, uint32_t v) {
    static const char hx[] = "0123456789ABCDEF";
    if (*n < lim) b[(*n)++] = hx[(v >> 4) & 0xF];
    if (*n < lim) b[(*n)++] = hx[v & 0xF];
}

static void k_emit(char *b, int n, int lim) {
    if (n < lim) b[n] = 0; else b[lim] = 0;
    dmesg_write(b);
}

/*
 * HID usage ID (Keyboard/Keypad page 0x07) -> 内核键码。
 * 返回 0 = 该键没有可注入的字符（CapsLock / 未知键）。
 */
static int hid_map(uint8_t k, int shift) {
    /* 0x04..0x1D: a..z（HID 键码是"物理键位"，布局无关） */
    if (k >= 0x04u && k <= 0x1Du) {
        char c = (char)('a' + (int)(k - 0x04u));
        return shift ? (int)(c - 'a' + 'A') : (int)c;
    }
    /* 0x1E..0x27: 1..9,0（主键盘数字行，shift 给出符号） */
    if (k >= 0x1Eu && k <= 0x27u) {
        static const char d[]  = "1234567890";
        static const char ds[] = "!@#$%^&*()";
        int i = (int)(k - 0x1Eu);
        return shift ? (int)ds[i] : (int)d[i];
    }
    if (k>=0x59 && k<=0x63) {
        static const char digits[]="1234567890.";
        static const int nav[]={KEY_END,KEY_DOWN,KEY_PGDN,KEY_LEFT,0,
                               KEY_RIGHT,KEY_HOME,KEY_UP,KEY_PGUP,0,0};
        /* Shift temporarily reverses Num Lock, as on a PC keypad. */
        return (num^shift) ? digits[k-0x59] : nav[k-0x59];
    }
    switch (k) {
    case 0x54: return '/';
    case 0x55: return '*';
    case 0x56: return '-';
    case 0x57: return '+';
    case 0x58: return '\n';
    case 0x67: return '=';
    case 0x28u: return (int)'\n';            /* Enter */
    case 0x29u: return 27;                   /* Esc */
    case 0x2Au: return (int)'\b';            /* Backspace */
    case 0x2Bu: return (int)'\t';            /* Tab */
    case 0x2Cu: return (int)' ';             /* Space */
    case 0x2Du: return shift ? '_'  : '-';
    case 0x2Eu: return shift ? '+'  : '=';
    case 0x2Fu: return shift ? '{'  : '[';
    case 0x30u: return shift ? '}'  : ']';
    case 0x31u: return shift ? '|'  : '\\';
    case 0x33u: return shift ? ':'  : ';';
    case 0x34u: return shift ? '"'  : '\'';
    case 0x35u: return shift ? '~'  : '`';
    case 0x36u: return shift ? '<'  : ',';
    case 0x37u: return shift ? '>'  : '.';
    case 0x38u: return shift ? '?'  : '/';
    case 0x39u: return 0;                    /* CapsLock：无字符 */
    case 0x4Au: return KEY_HOME;
    case 0x4Bu: return KEY_PGUP;
    case 0x4Du: return KEY_END;
    case 0x4Eu: return KEY_PGDN;
    case 0x4Fu: return KEY_RIGHT;
    case 0x50u: return KEY_LEFT;
    case 0x51u: return KEY_DOWN;
    case 0x52u: return KEY_UP;
    default: break;
    }
    /* 0x3A..0x45: F1..F12（KEY_F1 = -7，往后递减） */
    if (k >= 0x3Au && k <= 0x45u) return KEY_F1 - (int)(k - 0x3Au);
    return 0;
}

/* ---------- 入口：认领键盘并切 boot 协议 ---------- */
static const usb_hid_if_t *select_keys(const usb_dev_t *d) {
    if(!usbhc_supports_interrupt(&d->bus))return 0;
        for(unsigned j=0;j<d->hid_count;j++){
            const usb_hid_if_t *h=&d->hid[j];
            if(!h->ready || !usb_hid_keyboard(h))continue;
            uint8_t protocol=!!h->bitmap.bytes;
            uint8_t sp[]={0x21,0x0b,protocol,0,h->number,0,0,0};
            if(h->sub==1 && !h->key_layout &&
               usbhc_control_xfer(&d->bus,d->addr,0,sp,0,0,0,0)!=0) {
                uint8_t proto=255;
                int got=0;
                uint8_t get[]={0xa1,3,0,0,h->number,0,1,0};
                if(usbhc_control_xfer(&d->bus,d->addr,0,get,1,&proto,1,&got)!=0 ||
                   got!=1 || proto!=protocol)continue;
            }
            return h;
        }
    return 0;
}

static void bind_keyboard(kbd_t *k,const usb_dev_t *picked,const usb_hid_if_t *keys) {
    {
        char line[128];
        int n2 = 0;
        int lim = (int)sizeof(line) - 1;
        k_str(line, &n2, lim, keys->bitmap.bytes ? "USB-KBD: bitmap kbd addr=" :
              keys->key_layout ? "USB-KBD: report kbd addr=" : "USB-KBD: boot kbd addr=");
        k_dec(line, &n2, lim, (uint32_t)picked->addr);
        k_str(line, &n2, lim, " ep=");
        k_hex2(line, &n2, lim, (uint32_t)keys->ep);
        k_str(line, &n2, lim, " mps=");
        k_dec(line, &n2, lim, (uint32_t)keys->mps);
        k_str(line, &n2, lim, " int=");
        k_dec(line, &n2, lim, (uint32_t)keys->interval);
        k_str(line, &n2, lim, " proto=ok");
        k_emit(line, n2, lim);
    }
    uint32_t flags=irq_save_disable();
    *k=(kbd_t){.dev=picked,.keys=keys,.led_sent=-1};
    irq_restore(flags);
}

void usbkbd_init(void) {
    int n=usbenum_device_count();
    for(int i=0;i<n && i<USBENUM_MAX_DEV;i++){
        const usb_dev_t *d=usbenum_get(i);
        if(!d || !d->configured || keyboards[i].dev)continue;
        const usb_hid_if_t *keys=select_keys(d);
        if(keys)bind_keyboard(&keyboards[i],d,keys);
    }
    if(!usbkbd_present())dmesg_write("USB-KBD: no HID boot keyboard");
}

void usbkbd_detach(const usb_dev_t *device) {
    uint32_t flags=irq_save_disable();
    for(unsigned i=0;i<USBENUM_MAX_DEV;i++)
        if(keyboards[i].dev==device)keyboards[i]=(kbd_t){0};
    irq_restore(flags);
}

/* Called only by the hotplug worker. Each physical keyboard receives the
 * shared lock state, but failing/stalling LEDs don't block other keyboards. */
void usbkbd_sync_leds(void) {
    uint32_t flags=irq_save_disable();
    uint8_t bits=(uint8_t)((caps<<1)|num);
    irq_restore(flags);
    for(unsigned i=0;i<USBENUM_MAX_DEV;i++){
        kbd_t *k=&keyboards[i];
        const usb_dev_t *d=k->dev;
        const usb_hid_if_t *h=k->keys;
        if(!d || h->key_layout || h->bitmap.bytes || k->led_sent==bits || k->led_failed)continue;
        uint8_t request[8]={0x21,9,0,2,h->number,0,1,0};
        if(usbhc_control_xfer(&d->bus,d->addr,0,request,0,&bits,1,NULL)!=0){
            k->led_failed=1;
            continue;
        }
        k->led_sent=bits;
        char line[]="USB-KBD: LED report 0";
        line[sizeof(line)-2]=(char)('0'+bits);
        dmesg_record(line);
    }
}

static unsigned modifiers(void) {
    unsigned mod=0;
    for(unsigned i=0;i<USBENUM_MAX_DEV;i++)
        if(keyboards[i].dev)mod|=keyboards[i].modifiers;
    return mod;
}

static int repeatable(int key) {
    return (key>=32 && key<127) || key=='\b' || key==KEY_UP ||
           key==KEY_DOWN || key==KEY_LEFT || key==KEY_RIGHT ||
           key==KEY_PGUP || key==KEY_PGDN;
}
static int mapped(uint8_t code,uint8_t mod) {
    return code==0x2bu && (mod&0x44u) ? KEY_ALT_TAB :
        hid_map(code, !!(mod&0x22u) ^ (caps && code>=4 && code<=0x1d));
}

void usbkbd_cancel_repeat(void) {
    uint32_t flags=irq_save_disable();
    for(unsigned i=0;i<USBENUM_MAX_DEV;i++)keyboards[i].repeat=0;
    irq_restore(flags);
}

static void repeat_poll(kbd_t *k,uint32_t now) {
    if(k->repeat && (int32_t)(now-k->repeat_after)>=0){
        unsigned mod=modifiers();
        int key=mapped(k->repeat,(uint8_t)mod);
        if(!(mod&NON_SHIFT_MODS) && repeatable(key))keyboard_inject(key);
        k->repeat_after=now+REPEAT_PERIOD;
    }
}

/* Bounded polling; serialize the previous report, repeat, and injection. */
static unsigned report_bit(const uint8_t *data,unsigned bit){
    return (data[bit>>3]>>(bit&7u))&1u;
}

static void poll_report(kbd_t *k,uint32_t now) {
    const usb_dev_t *d=k->dev;
    uint8_t data[64]={0};
    const hid_key_bitmap_t *layout=&k->keys->bitmap;
    unsigned id=layout->bytes ? layout->id : (k->keys->key_layout<256 ? k->keys->key_layout : 0);
    int act=0, prefix=!!id;
    int want=layout->bytes ? layout->bytes : (int)USBKBD_REPORT_LEN+prefix;
    uint8_t *rep=data+prefix;
    int r=usbhc_interrupt_in(&d->bus,d->addr,k->keys->ep,data,want,&act);
    if (r != 0 || (act && (act!=want || (prefix && data[0]!=id)))) {
        k->repeat=0;
        if(r!=0){ k->modifiers=0; for(unsigned i=0;i<32;i++)k->previous[i]=0; }
        return;
    }
    if (!act) {
        repeat_poll(k,now);
        return;
    }

    if (!k->got_report) {
        char line[96];
        int n = 0;
        int lim = (int)sizeof(line) - 1;
        k->got_report = 1;
        k_str(line, &n, lim, "USB-KBD: report ok len=");
        k_dec(line, &n, lim, (uint32_t)act);   /* 实测长度，不写死 8 */
        k_emit(line, n, lim);
    }

    uint8_t keys[32]={0};
    unsigned mod=0;
    if(layout->bytes){
        for(unsigned k=layout->first;k<=layout->last;k++){
            if(!report_bit(rep,layout->key_bit+k-layout->first))continue;
            if(k>=224)mod|=1u<<(k-224);
            else if(k>=4)keys[k>>3]|=(uint8_t)(1u<<(k&7u));
        }
        if(layout->mod_bit!=0xffff)
            for(unsigned k=0;k<8;k++)mod|=report_bit(rep,layout->mod_bit+k)<<k;
    }else{
        mod=rep[0];
        /* ErrorRollOver/POSTFail/Undefined are not releases. */
        for(unsigned i=2;i<USBKBD_REPORT_LEN;i++){
            unsigned usage=rep[i];
            if(usage && usage<=3){k->repeat=0;return;}
            if(usage)keys[usage>>3]|=(uint8_t)(1u<<(usage&7u));
        }
    }
    k->modifiers=(uint8_t)mod;
    mod=modifiers();
    if(!report_bit(keys,k->repeat))k->repeat=0;
    unsigned end=layout->bytes?224u:USBKBD_REPORT_LEN;
    for(unsigned i=layout->bytes?4u:2u;i<end;i++){
        unsigned code=layout->bytes?i:rep[i];
        if(!code || !report_bit(keys,code) || report_bit(k->previous,code))continue;
        /* Also suppress duplicate array entries within one report. */
        k->previous[code>>3]|=(uint8_t)(1u<<(code&7u));

        if(code==0x39u){caps=!caps;continue;}
        if(code==0x53u){num=!num;continue;}
        int key=mapped(code,mod);
        if (repeatable(key) && !(mod&NON_SHIFT_MODS)) {
            k->repeat=code;
            k->repeat_after=now+REPEAT_DELAY;
        }
        if (!keyboard_private_input() && !user_managed()) {
            char line[128];
            int n = 0;
            int lim = (int)sizeof(line) - 1;
            k_str(line, &n, lim, "USB-KBD: key=");
            k_dec(line, &n, lim, (uint32_t)key);
            k_str(line, &n, lim, " mod=");
            k_hex2(line, &n, lim, (uint32_t)mod);
            k_emit(line, n, lim);
        }
        if (key != 0) keyboard_inject(key);
    }

    for(unsigned i=0;i<sizeof(keys);i++)k->previous[i]=keys[i];
    repeat_poll(k,now);
}

void usbkbd_poll(void) {
    uint32_t flags=irq_save_disable();
    if(!flags)return;
    uint32_t now=g_pit_ticks;
    if((uint32_t)(now-last_poll)>=USBKBD_THROTTLE_MS){
        last_poll=now;
        for(unsigned i=0;i<USBENUM_MAX_DEV;i++)
            if(keyboards[i].dev)poll_report(&keyboards[i],now);
    }
    irq_restore(flags);
}

int usbkbd_present(void) {
    for(unsigned i=0;i<USBENUM_MAX_DEV;i++)if(keyboards[i].dev)return 1;
    return 0;
}

int usbkbd_got_report(void) {
    for(unsigned i=0;i<USBENUM_MAX_DEV;i++)if(keyboards[i].got_report)return 1;
    return 0;
}
