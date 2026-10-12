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
 * 能力差异（fail closed）：EHCI 目前没有周期调度，usbhc_interrupt_in
 * 对它直接返回 -1，类驱动靠 usbhc_supports_interrupt() 在认领阶段就
 * 跳过（而不是等到运行时每条轮询都失败刷屏）。
 */
#include "usbhc.h"
#include "uhci.h"
#include "ehci.h"
#include "dmesg.h"
#include "types.h"

static usbhc_t g_ports[USBHC_MAX_PORTS];
static int     g_ports_n = 0;

const char *usbhc_name(uint8_t hc) {
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
    if (g_ports_n < nu + ne) {
        char w[128];
        int m = 0;
        int wl = (int)sizeof(w) - 1;
        const char *t = "USB-HC: WARNING port table truncated (";
        while (*t && m < wl) w[m++] = *t++;
        m = usbhc_put_dec(w, m, wl, (uint32_t)g_ports_n);
        t = " of ";
        while (*t && m < wl) w[m++] = *t++;
        m = usbhc_put_dec(w, m, wl, (uint32_t)(nu + ne));
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
        /* 手写十进制（内核无 sprintf）。用同一个辅助函数，别再写
         * `(char)('0' + (n > 9 ? 9 : n))` —— 那样 16 口会打成 "9"，
         * 日志比不打印还误导。 */
        n = usbhc_put_dec(line, n, lim, (uint32_t)g_ports_n);
        s = " connected port(s) (UHCI ";
        while (*s && n < lim) line[n++] = *s++;
        n = usbhc_put_dec(line, n, lim, (uint32_t)nu);
        s = " / EHCI ";
        while (*s && n < lim) line[n++] = *s++;
        n = usbhc_put_dec(line, n, lim, (uint32_t)ne);
        if (n < lim) line[n++] = ')';
        if (n < lim) line[n] = 0; else line[lim] = 0;
        dmesg_write(line);
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
    if (h->hc == USBHC_EHCI) return ehci_port_reset((int)h->ctl, (int)h->port);
    return uhci_port_reset(h->io, (int)h->port);
}

int usbhc_hc_start(const usbhc_t *h) {
    if (!h) return -1;
    if (h->hc == USBHC_EHCI) return 0;      /* 异步调度常驻，无需重启 */
    return uhci_hc_start(h->io);
}

int usbhc_control_xfer(const usbhc_t *h, uint8_t addr, uint8_t ep,
                       const uint8_t *setup, int dir_in,
                       uint8_t *buf, int blen, int *actlen) {
    if (!h) return -1;
    if (h->hc == USBHC_EHCI)
        return ehci_control_xfer((int)h->ctl, addr, ep, setup, dir_in,
                                 buf, blen, actlen);
    return uhci_control_xfer(h->io, addr, ep, setup, dir_in,
                             buf, blen, h->ls ? 1 : 0, actlen);
}

int usbhc_interrupt_in(const usbhc_t *h, uint8_t addr, uint8_t ep,
                       uint8_t *buf, int blen, int *actlen) {
    if (!h) return -1;
    if (h->hc == USBHC_EHCI) return -1;     /* 周期调度尚未实现 */
    return uhci_interrupt_in(h->io, addr, ep, buf, blen,
                             h->ls ? 1 : 0, actlen);
}

int usbhc_bulk_xfer(const usbhc_t *h, uint8_t addr, uint8_t ep,
                    uint8_t *buf, int blen, int mps, int *actlen) {
    if (!h) return -1;
    if (h->hc == USBHC_EHCI)
        return ehci_bulk_xfer((int)h->ctl, addr, ep, buf, blen, mps, actlen);
    return uhci_bulk_xfer(h->io, addr, ep, buf, blen, mps,
                          h->ls ? 1 : 0, actlen);
}

void usbhc_bulk_tog_reset(const usbhc_t *h, uint8_t addr) {
    if (!h) return;
    if (h->hc == USBHC_EHCI) {
        ehci_bulk_tog_reset((int)h->ctl, addr);
        return;
    }
    uhci_bulk_tog_reset(addr);
}

int usbhc_supports_interrupt(const usbhc_t *h) {
    if (!h) return 0;
    return (h->hc == USBHC_UHCI) ? 1 : 0;
}
