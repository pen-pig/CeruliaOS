/*
 * usbmouse.c - USB HID 鼠标：boot 报告解析 + 轮询（真机点亮 H2-2e）
 *
 * 报告格式（USB HID boot interface，鼠标协议）：
 *   [0]     按键位图：bit0 左键 bit1 右键 bit2 中键（bit3..7 保留/填充）
 *   [1]     X 位移，int8（正 = 向右）
 *   [2]     Y 位移，int8（正 = 向下）
 *   [3]     Wheel 位移，int8（可选，只有 >=4 字节的报告才认）
 *
 * 与 PS/2 的坐标差异必须在这一层吃掉：PS/2 的 Y 正值是"向上"，而 USB boot
 * 报告的 Y 正值是"向下"。统一成屏幕坐标（dy>0 向下）后再交给 mouse 层，
 * 这样 GUI 侧完全不需要知道指针来自哪条总线。
 *
 * 红线落实：
 *  - 不可信输入上界：报告不足 3 字节直接丢弃；缓冲固定 8 字节，mps 夹紧。
 *  - 轮询使用非阻塞 USB IN；5ms 节流，避免反复读取同一端点。
 *  - 与 PS/2 共用状态：注入走 mouse_inject_report()，由 mouse.c 负责关中断
 *    保护，本模块不直接碰指针坐标。
 *  - 静默原则：NAK（没有新报告）是常态，绝不打印；诊断行只在真的收到报告
 *    或真的有位移/按键时打，并对"移动事件"再做 250ms 节流防刷屏。
 */
#include "usbmouse.h"
#include "types.h"
#include "usbwheel.h"
#include "usbenum.h"
#include "usbhc.h"
#include "mouse.h"
#include "isr.h"
#include "irqflags.h"
#include "dmesg.h"
#include "types.h"

#define USBMOU_THROTTLE_MS 5u      /* 轮询节流：鼠标 interval 通常 10ms */
#define USBMOU_REPORT_MIN  3u      /* boot 鼠标报告最小 3 字节 */
#define USBMOU_REPORT_LEN  8u      /* 缓冲上界 */
#define USBMOU_EMIT_MS     250u    /* 移动诊断行节流 */

typedef struct {
    const usb_dev_t *dev;
    const usb_hid_if_t *pointer;
    uint32_t last_emit;
    uint16_t abs_x,abs_y;
    uint8_t mode, buttons, got_report, abs_valid;
} pointer_t;
static pointer_t pointers[USBENUM_MAX_DEV];
static uint32_t last_poll;

static int usb_buttons(void) {
    int merged=0;
    for(unsigned i=0;i<USBENUM_MAX_DEV;i++)
        if(pointers[i].dev)merged|=pointers[i].buttons;
    return merged;
}

static void m_str(char *b, int *n, int lim, const char *s) {
    while (*s && *n < lim) b[(*n)++] = *s++;
}

static void m_dec(char *b, int *n, int lim, uint32_t v) {
    char t[12];
    int m = 0;
    if (v == 0) t[m++] = '0';
    while (v) { t[m++] = (char)('0' + v % 10); v /= 10; }
    while (m && *n < lim) b[(*n)++] = t[--m];
}

/* 有符号十进制（位移是 int8，可能为负） */
static void m_sdec(char *b, int *n, int lim, int v) {
    uint32_t u = (v < 0) ? (uint32_t)(0u - (uint32_t)v) : (uint32_t)v;
    if (v < 0) m_str(b, n, lim, "-");
    m_dec(b, n, lim, u);
}

static void m_emit(char *b, int n, int lim) {
    if (n < lim) b[n] = 0; else b[lim] = 0;
    dmesg_write(b);
}

/* ---------- 入口：认领鼠标并切 boot 协议 ---------- */
static const usb_hid_if_t *select_pointer(const usb_dev_t *d,int *is_boot,unsigned *report_mode) {
    if(!usbhc_supports_interrupt(&d->bus))return 0;
        for(unsigned j=0;j<d->hid_count;j++){
            const usb_hid_if_t *candidate=&d->hid[j];
            if(!candidate->ready || !usb_hid_pointer(candidate))continue;
            int boot=candidate->sub==1 && candidate->proto==2;
            unsigned mode=candidate->pointer_layout;
            if(boot){
                uint8_t sp[]={0x21,0x0b,(uint8_t)!!mode,0,candidate->number,0,0,0};
                int result=usbhc_control_xfer(&d->bus,d->addr,0,sp,0,0,0,0);
                if(result && mode) {
                    sp[2]=0;
                    result=usbhc_control_xfer(&d->bus,d->addr,0,sp,0,0,0,0);
                }
                if(result) {
                    /* Some receivers decline SET_PROTOCOL when already in the
                     * requested mode. Don't guess: ask before interpreting. */
                    uint8_t get[]={0xa1,3,0,0,candidate->number,0,1,0},proto=255;
                    int got=0;
                    if(usbhc_control_xfer(&d->bus,d->addr,0,get,1,&proto,1,&got)!=0 ||
                       got!=1 || proto>1 || (proto && !mode))continue;
                    sp[2]=proto;
                }
                if(!sp[2])mode=0;
            }
            *is_boot=boot;
            *report_mode=mode;
            return candidate;
        }
    return 0;
}

static void bind_pointer(pointer_t *m,const usb_dev_t *picked,const usb_hid_if_t *h,
                         int boot,unsigned report_mode) {
    {
        char line[128];
        int n2 = 0;
        int lim = (int)sizeof(line) - 1;
        m_str(line, &n2, lim, boot ? "USB-MOU: boot mouse addr=" : "USB-MOU: report pointer addr=");
        m_dec(line, &n2, lim, (uint32_t)picked->addr);
        m_str(line, &n2, lim, " ep=");
        m_dec(line, &n2, lim, (uint32_t)h->ep);
        m_str(line, &n2, lim, " mps=");
        m_dec(line, &n2, lim, (uint32_t)h->mps);
        m_str(line, &n2, lim, " int=");
        m_dec(line, &n2, lim, (uint32_t)h->interval);
        m_str(line, &n2, lim, report_mode==HID_ABS_WHEEL ? " proto=ok mode=absolute" :
              report_mode ? " proto=ok mode=wheel" : " proto=ok mode=boot");
        m_emit(line, n2, lim);
    }
    uint32_t flags=irq_save_disable();
    *m=(pointer_t){.dev=picked,.pointer=h,.mode=(uint8_t)report_mode};
    mouse_usb_set_present(1);
    irq_restore(flags);
}

void usbmouse_init(void) {
    int n=usbenum_device_count();
    for(int i=0;i<n && i<USBENUM_MAX_DEV;i++){
        const usb_dev_t *d=usbenum_get(i);
        if(!d || !d->configured || pointers[i].dev)continue;
        int boot=0;
        unsigned mode=0;
        const usb_hid_if_t *h=select_pointer(d,&boot,&mode);
        if(h)bind_pointer(&pointers[i],d,h,boot,mode);
    }
    if(!usbmouse_present())dmesg_write("USB-MOU: no HID boot mouse (or usable report-mode pointer)");
}

void usbmouse_detach(const usb_dev_t *device) {
    uint32_t flags=irq_save_disable();
    int old=usb_buttons();
    for(unsigned i=0;i<USBENUM_MAX_DEV;i++)
        if(pointers[i].dev==device)pointers[i]=(pointer_t){0};
    int new=usb_buttons();
    if(old!=new)mouse_inject_report(0,0,new,0);
    mouse_usb_set_present(usbmouse_present());
    irq_restore(flags);
}

/* Read a report and publish any button, motion or wheel change. */
static void poll_once(pointer_t *m) {
    const usb_dev_t *d=m->dev;
    uint8_t data[USBMOU_REPORT_LEN];
    uint8_t *rep=data;
    int act = 0;
    int mps = (int)m->pointer->mps;
    if (mps < (int)USBMOU_REPORT_MIN) mps = (int)USBMOU_REPORT_MIN;
    if (mps > (int)sizeof(data)) mps = (int)sizeof(data);

    for (int i = 0; i < (int)sizeof(data); i++) rep[i] = 0;
    int r = usbhc_interrupt_in(&d->bus, d->addr, m->pointer->ep, rep, mps, &act);
    if (r != 0) {
        /* Losing a device mid-drag must not leave the desktop captured. */
        if (m->buttons) {m->buttons=0;mouse_inject_report(0,0,usb_buttons(),0);}
        m->abs_valid=0;
        return;
    }
    if (act < (int)USBMOU_REPORT_MIN) return;    /* NAK 或报告不完整：静默 */
    if(m->mode && m->pointer->pointer_id){
        if(rep[0]!=m->pointer->pointer_id)return;
        rep++;act--;
    }
    if(m->mode && (unsigned)act!=hid_pointer_bytes(m->mode))return;

    if (!m->got_report) {
        char line[96];
        int n = 0;
        int lim = (int)sizeof(line) - 1;
        m->got_report = 1;
        m_str(line, &n, lim, "USB-MOU: report ok len=");
        m_dec(line, &n, lim, (uint32_t)act);   /* 实测长度，不写死 */
        m_emit(line, n, lim);
    }

    /*
     * 解析走 mouse_decode_usb() 这个纯函数：与 PS/2 共用同一份屏幕坐标语义
     * （dx>0 右、dy>0 下、dz>0 上滚），并被 mouse_selftest 逐条守护——
     * QEMU 没有触摸板也不发滚轮事件，真机上字节解析差一位只能靠它兜住。
     */
    mouse_ev_t ev;
    ev.dx = ev.dy = ev.buttons = ev.dz = 0;
    if(m->mode==HID_ABS_WHEEL) {
        if(act!=6)return;
        unsigned x=rep[1]|((unsigned)rep[2]<<8),y=rep[3]|((unsigned)rep[4]<<8);
        if(x>32767 || y>32767)return;
        int buttons=rep[0]&7,delta=(int8_t)rep[5],changed=m->buttons!=buttons;
        m->buttons=(uint8_t)buttons;
        if(!m->abs_valid || x!=m->abs_x || y!=m->abs_y){
            m->abs_valid=1;
            m->abs_x=(uint16_t)x;
            m->abs_y=(uint16_t)y;
            mouse_inject_absolute(x,y,usb_buttons(),delta);
        }else if(changed || delta){
            /* Don't move the shared cursor back to a stationary tablet. */
            mouse_inject_report(0,0,usb_buttons(),delta);
        }
        return;
    }
    if(m->mode==HID_REL16 || m->mode==HID_REL16_WHEEL){
        ev.buttons=rep[0]&7;
        ev.dx=(int16_t)(rep[1]|((unsigned)rep[2]<<8));
        ev.dy=(int16_t)(rep[3]|((unsigned)rep[4]<<8));
        if(m->mode==HID_REL16_WHEEL)ev.dz=(int8_t)rep[5];
    }else if (mouse_decode_usb(rep, m->mode ? (uint32_t)act : 3u, &ev) != 0) return;

    int btn = ev.buttons;
    int dx  = ev.dx;
    int dy  = ev.dy;
    int dz  = ev.dz;

    /* 静止且按键没变：不是事件，直接返回（避免把 0 位移当事件刷屏） */
    if (dx == 0 && dy == 0 && dz == 0 && btn == m->buttons) return;
    m->buttons = btn;

    /*
     * 注入：统一成屏幕坐标交给 mouse 层。
     *   X：USB 正 = 右，屏幕 X 正 = 右 -> 原样
     *   Y：USB 正 = 下，屏幕 Y 正 = 下 -> 原样（mouse 层内部已按屏幕坐标累加）
     * 滚轮：USB 报告正 = 上滚，mouse 层的 wheel 语义是 >0 向上滚 -> 原样
     */
    mouse_inject_report(dx,dy,usb_buttons(),dz);

    if (m->last_emit && (uint32_t)(g_pit_ticks - m->last_emit) < USBMOU_EMIT_MS) return;
    m->last_emit = g_pit_ticks;

    {
        char line[128];
        int n = 0;
        int lim = (int)sizeof(line) - 1;
        m_str(line, &n, lim, "USB-MOU: move dx=");
        m_sdec(line, &n, lim, dx);
        m_str(line, &n, lim, " dy=");
        m_sdec(line, &n, lim, dy);
        m_str(line, &n, lim, " btn=");
        m_dec(line, &n, lim, (uint32_t)mouse_get_buttons());
        m_str(line, &n, lim, " x=");
        m_dec(line, &n, lim, (uint32_t)mouse_get_x());
        m_str(line, &n, lim, " y=");
        m_dec(line, &n, lim, (uint32_t)mouse_get_y());
        m_emit(line, n, lim);
    }
}

/* Nonblocking per-device polls; merged button state survives a detach. */
void usbmouse_poll(void) {
    uint32_t flags=irq_save_disable();
    if(!flags)return;
    uint32_t now=g_pit_ticks;
    if((uint32_t)(now-last_poll)>=USBMOU_THROTTLE_MS){
        last_poll=now;
        for(unsigned i=0;i<USBENUM_MAX_DEV;i++)
            if(pointers[i].dev)poll_once(&pointers[i]);
    }
    irq_restore(flags);
}

int usbmouse_present(void) {
    for(unsigned i=0;i<USBENUM_MAX_DEV;i++)if(pointers[i].dev)return 1;
    return 0;
}

int usbmouse_got_report(void) {
    for(unsigned i=0;i<USBENUM_MAX_DEV;i++)if(pointers[i].got_report)return 1;
    return 0;
}
