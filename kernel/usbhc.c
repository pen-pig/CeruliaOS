/*
 * usbhc.c - USB 主机控制器传输层统一入口（真机点亮 A1 第二阶段）
 *
 * 只做分派，不碰寄存器：把"设备挂在哪种控制器上"这个事实从类驱动里
 * 拿走，让 usbenum/usbkbd/usbmouse/usbmsc 只认 usbhc_t 这一个句柄。
 *
 * 端口表是静态的（USBHC_MAX_PORTS 条），在 usbhc_scan() 里一次性合并
 * UHCI 与 EHCI 两边已经各自探测好的已连接端口；之后只读，无动态分配、
 * 无失败回滚问题。
 *
 * Controller dispatch for control, HID interrupt IN and supported bulk I/O.
 */
#include "usbhc.h"
#include "uhci.h"
#include "ehci.h"
#include "xhci.h"
#include "ohci.h"
#include "dmesg.h"
#include "types.h"

static usbhc_t g_ports[USBHC_MAX_PORTS];
static int     g_ports_n = 0;

const char *usbhc_name(uint8_t hc) {
    if (hc == USBHC_XHCI) return "xHCI";
    if (hc == USBHC_OHCI) return "OHCI";
    return (hc == USBHC_EHCI) ? "EHCI" : "UHCI";
}

/* 追加十进制表示，返回新的写入位置。内核无 sprintf。 */
static int usbhc_put_dec(char *buf, int n, int lim, uint32_t v) {
    char t[10];
    int m = 0;
    if (v == 0u) {
        if (n < lim) buf[n++] = '0';
        return n;
    }
    while (v && m < (int)sizeof(t)) { t[m++] = (char)('0' + v % 10u); v /= 10u; }
    while (m && n < lim) buf[n++] = t[--m];
    return n;
}

void usbhc_scan(void) {
    g_ports_n = 0;

    int nu = uhci_port_count();
    for (int i = 0; i < nu && g_ports_n < USBHC_MAX_PORTS; i++) {
        uint16_t io = 0;
        int port = 0;
        int ls = 0;
        if (uhci_port_get(i, &io, &port, &ls) != 0) continue;
        g_ports[g_ports_n].hc   = USBHC_UHCI;
        g_ports[g_ports_n].ctl  = 0;
        g_ports[g_ports_n].port = (uint8_t)port;
        g_ports[g_ports_n].ls   = (uint8_t)(ls ? 1 : 0);
        g_ports[g_ports_n].io   = io;
        g_ports_n++;
    }

    int nx=0;
    for(unsigned ctl=0;ctl<XHCI_MAX_CTL;ctl++){
        int found=xhci_port_count(ctl);
        nx+=found;
        for(int i=0;i<found && g_ports_n<USBHC_MAX_PORTS;i++){
            int port=xhci_port_get(ctl,i);
            if(port<0)continue;
            g_ports[g_ports_n++]=(usbhc_t){.hc=USBHC_XHCI,.ctl=(uint8_t)ctl,.port=(uint8_t)port};
        }
    }

    int no=0;
    for(unsigned i=0;i<OHCI_MAX_CTL;i++)
        for(unsigned p=0;p<ohci_root_count(i);p++){
            unsigned st=ohci_root_status(i,p,0);
            if(!(st&1u))continue;
            no++;
            if(g_ports_n>=USBHC_MAX_PORTS)continue;
            g_ports[g_ports_n++]=(usbhc_t){.hc=USBHC_OHCI,.ctl=(uint8_t)i,
                                      .port=(uint8_t)p,.ls=!!(st&4u)};
        }
    int ne = ehci_port_count();
    for (int i = 0; i < ne && g_ports_n < USBHC_MAX_PORTS; i++) {
        int ctl = 0;
        int port = 0;
        if (ehci_port_get(i, &ctl, &port) != 0) continue;
        if (ctl < 0 || ctl >= EHCI_MAX_CTL) continue;   /* 不可信上界 */
        g_ports[g_ports_n].hc   = USBHC_EHCI;
        g_ports[g_ports_n].ctl  = (uint8_t)ctl;
        g_ports[g_ports_n].port = (uint8_t)port;
        g_ports[g_ports_n].ls   = 0;
        g_ports[g_ports_n].io   = 0;
        g_ports_n++;
    }

    /* 登记数与控制器实际口数应当一致。不一致说明表被截断了（容量不足或
     * port_get 失败被跳过），必须打出来 —— 否则表现只是"某个设备不认"，
     * 事后无从判断是设备没插还是我们丢了。 */
    if (g_ports_n < nu + ne + nx + no) {
        char w[128];
        int m = 0;
        int wl = (int)sizeof(w) - 1;
        const char *t = "USB-HC: WARNING port table truncated (";
        while (*t && m < wl) w[m++] = *t++;
        m = usbhc_put_dec(w, m, wl, (uint32_t)g_ports_n);
        t = " of ";
        while (*t && m < wl) w[m++] = *t++;
        m = usbhc_put_dec(w, m, wl, (uint32_t)(nu + ne + nx + no));
        t = " dropped)";
        while (*t && m < wl) w[m++] = *t++;
        w[m] = 0;
        dmesg_write(w);
    }

    {
        char line[128];
        int n = 0;
        int lim = (int)sizeof(line) - 1;
        const char *s = "USB-HC: ";
        while (*s && n < lim) line[n++] = *s++;
        n = usbhc_put_dec(line, n, lim, (uint32_t)g_ports_n);
        s = " connected port(s) (UHCI ";
        while (*s && n < lim) line[n++] = *s++;
        n = usbhc_put_dec(line, n, lim, (uint32_t)nu);
        s = " / EHCI ";
        while (*s && n < lim) line[n++] = *s++;
        n = usbhc_put_dec(line, n, lim, (uint32_t)ne);
        if(no){
            s=" / OHCI ";
            while(*s && n<lim)line[n++]=*s++;
            n=usbhc_put_dec(line,n,lim,(uint32_t)no);
        }
        if (nx) {
            s = " / xHCI ";
            while (*s && n < lim) line[n++] = *s++;
            n = usbhc_put_dec(line, n, lim, (uint32_t)nx);
        }
        if (n < lim) line[n++] = ')';
        if (n < lim) line[n] = 0; else line[lim] = 0;
        if (g_ports_n > 0 || nu > 0 || ne > 0) dmesg_write(line);
        /* 全零 = 无任何 USB 控制器，usb_scan_log 已报，不重复 */
    }
}

int usbhc_port_count(void) {
    return g_ports_n;
}

int usbhc_port_get(int i, usbhc_t *out) {
    if (i < 0 || i >= g_ports_n) return -1;
    if (out) *out = g_ports[i];
    return 0;
}

int usbhc_port_reset(const usbhc_t *h) {
    if (!h) return 0;
    if (h->hc == USBHC_XHCI) return xhci_port_start(h->ctl,h->port);
    if (h->hc == USBHC_OHCI) return ohci_reset(h->ctl&127u,h->port,h->ctl&USBHC_DOWNSTREAM);
    if (h->hc == USBHC_EHCI) return h->hub_port ? 1 : ehci_port_reset(h->ctl,h->port);
    return h->ctl ? 1 : uhci_port_reset(h->io, (int)h->port);
}

int usbhc_hc_start(const usbhc_t *h) {
    if (!h) return -1;
    if (h->hc == USBHC_EHCI || h->hc == USBHC_XHCI || h->hc==USBHC_OHCI) return 0;
    return uhci_hc_start(h->io);
}

int usbhc_control_xfer(const usbhc_t *h, uint8_t addr, uint8_t ep,
                       const uint8_t *setup, int dir_in,
                       uint8_t *buf, int blen, int *actlen) {
    if (!h) return -1;
    if (h->hc == USBHC_XHCI)
        return ep == 0 ? xhci_control(h->ctl,h->port, setup, dir_in, buf, blen, actlen) : -1;
    if(h->hc==USBHC_OHCI)
        return !ep ? ohci_control(h->ctl&127u,addr,h->ls,setup,dir_in,buf,blen,actlen) : -1;
    if (h->hc == USBHC_EHCI)
        return h->tt_hub ? ehci_control_split(h->ctl,addr,ep,h->tt_hub,h->hub_port,
                                             h->ls,setup,dir_in,buf,blen,actlen)
                         : ehci_control_xfer(h->ctl,addr,ep,setup,dir_in,buf,blen,actlen);
    return uhci_control_xfer(h->io, addr, ep, setup, dir_in,
                             buf, blen, h->ls ? 1 : 0, actlen);
}

int usbhc_interrupt_in(const usbhc_t *h, uint8_t addr, uint8_t ep,
                       uint8_t *buf, int blen, int *actlen) {
    if (!h) return -1;
    if (h->hc == USBHC_XHCI) return xhci_interrupt_in(h->ctl,h->port, ep, buf, blen, actlen);
    if(h->hc==USBHC_OHCI)return ohci_interrupt_in(h->ctl&127u,h->port,addr,ep,buf,blen,actlen);
    if (h->hc == USBHC_EHCI)
        return ehci_interrupt_in(h->ctl,h->port,addr,ep,buf,blen,actlen);
    return uhci_interrupt_in(h->io, h->port, addr, ep, buf, blen,
                             h->ls ? 1 : 0, actlen);
}

int usbhc_configure_hid(const usbhc_t *h, uint8_t addr, uint8_t ep, uint16_t mps, uint8_t interval) {
    if (!h) return -1;
    if(h->hc==USBHC_OHCI)return ohci_configure_hid(h->ctl&127u,addr,ep,mps,interval,h->ls);
    if(h->hc==USBHC_EHCI)
        return ehci_configure_hid(h->ctl,h->port,addr,ep,mps,interval,h->tt_hub,h->hub_port,h->ls);
    return h->hc == USBHC_XHCI ? xhci_configure_hid(h->ctl,h->port, ep, mps, interval) : 0;
}

int usbhc_bulk_xfer(const usbhc_t *h, uint8_t addr, uint8_t ep,
                    uint8_t *buf, int blen, int mps, int *actlen) {
    if (!usbhc_supports_bulk(h)) return -1;
    if (h->hc == USBHC_EHCI)
        return ehci_bulk_xfer((int)h->ctl, addr, ep, buf, blen, mps, actlen);
    return uhci_bulk_xfer(h->io, addr, ep, buf, blen, mps,
                          h->ls ? 1 : 0, actlen);
}

void usbhc_bulk_tog_reset(const usbhc_t *h, uint8_t addr) {
    if (!usbhc_supports_bulk(h)) return;
    if (h->hc == USBHC_EHCI) {
        ehci_bulk_tog_reset((int)h->ctl, addr);
        return;
    }
    uhci_bulk_tog_reset(h->io,addr);
}

int usbhc_supports_interrupt(const usbhc_t *h) {
    return h && (h->hc == USBHC_UHCI || h->hc == USBHC_XHCI ||
                 h->hc == USBHC_OHCI || h->hc == USBHC_EHCI);
}

int usbhc_supports_bulk(const usbhc_t *h) {
    return h && (h->hc == USBHC_UHCI || (h->hc == USBHC_EHCI && !h->hub_port));
}
