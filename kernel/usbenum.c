/*
 * usbenum.c - USB 设备枚举与描述符解析（真机点亮 H2-2c）
 *
 * 流程与边界见 usbenum.h。这里只补充实现层面的要点：
 *  - 本模块不碰任何主机控制器寄存器，全部经 usbhc_control_xfer() /
 *    usbhc_port_reset() / usbhc_hc_start() 三个原语完成（usbhc 层按设备
 *    所属控制器分派到 UHCI 或 EHCI），保持"传输层 / 枚举层"分层。
 *  - 每步失败都打印一条带端口号的失败行并放弃该端口（fail closed），
 *    绝不在没拿到真实描述符的情况下报成功——E2E 靠"插/不插设备结果相反"
 *    来证明这一点。
 *  - 描述符解析把"长度字段"当不可信输入：先夹紧再当下标；解析循环遇到
 *    len<2 或越界立即跳出，杜绝死循环与越界读。
 *  - 没有 sprintf，行缓冲 128 字节、所有写入以上界为准，末尾保 '\0'。
 */
#include "usbenum.h"
#include "task.h"
#include "irqflags.h"
#include "xhci.h"
#include "uhci.h"
#include "ohci.h"
#include "ehci.h"
#include "usbkbd.h"
#include "usbmouse.h"
#include "usbwheel.h"
#include "usbkeylayout.h"
#include "usbhc.h"
#include "isr.h"
#include "dmesg.h"

static usb_dev_t g_dev[USBENUM_MAX_DEV];
static int       g_dev_n = 0;

/* ---------- 手写格式化（与 uhci.c 同风格） ---------- */
static void e_str(char *b, int *n, int lim, const char *s) {
    while (*s && *n < lim) b[(*n)++] = *s++;
}

static void e_dec(char *b, int *n, int lim, uint32_t v) {
    char t[12];
    int m = 0;
    if (v == 0) t[m++] = '0';
    while (v) { t[m++] = (char)('0' + v % 10); v /= 10; }
    while (m && *n < lim) b[(*n)++] = t[--m];
}

static void e_hex(char *b, int *n, int lim, uint32_t v, int digits) {
    static const char hx[] = "0123456789ABCDEF";
    for (int s = (digits - 1) * 4; s >= 0; s -= 4) {
        if (*n < lim) b[(*n)++] = hx[(v >> s) & 0xF];
    }
}

/* 终结行缓冲并写进 dmesg（H1a 起 dmesg 全量镜像 COM1） */
static void e_emit(char *b, int n, int lim) {
    if (n < lim) b[n] = 0; else b[lim] = 0;
    dmesg_write(b);
}

/* 统一失败行：USB-ENUM: port<n> <stage> failed act=<k> */
static void e_fail(int port, const char *stage, int act) {
    char line[128];
    int n = 0;
    int lim = (int)sizeof(line) - 1;
    e_str(line, &n, lim, "USB-ENUM: port");
    e_dec(line, &n, lim, (uint32_t)port);
    e_str(line, &n, lim, " ");
    e_str(line, &n, lim, stage);
    e_str(line, &n, lim, " failed act=");
    e_dec(line, &n, lim, (uint32_t)act);
    e_emit(line, n, lim);
}

/* 真实时间忙等（g_pit_ticks 由 IRQ0 驱动，1ms/格） */
static void e_delay_ms(uint32_t ms) {
    uint32_t t0 = g_pit_ticks;
    while ((uint32_t)(g_pit_ticks - t0) < ms) { }
}

/* 设备描述符打印行 */
static void e_log_dev(int port, const usb_dev_t *d) {
    char line[128];
    int n = 0;
    int lim = (int)sizeof(line) - 1;
    e_str(line, &n, lim, "USB-ENUM: port");
    e_dec(line, &n, lim, (uint32_t)port);
    e_str(line, &n, lim, " addr=");
    e_dec(line, &n, lim, (uint32_t)d->addr);
    e_str(line, &n, lim, " vid=0x");
    e_hex(line, &n, lim, (uint32_t)d->vid, 4);
    e_str(line, &n, lim, " pid=0x");
    e_hex(line, &n, lim, (uint32_t)d->pid, 4);
    e_str(line, &n, lim, " cls=");
    e_hex(line, &n, lim, (uint32_t)d->cls, 2);
    e_str(line, &n, lim, " sub=");
    e_hex(line, &n, lim, (uint32_t)d->sub, 2);
    e_str(line, &n, lim, " proto=");
    e_hex(line, &n, lim, (uint32_t)d->proto, 2);
    e_str(line, &n, lim, " mps0=");
    e_dec(line, &n, lim, (uint32_t)d->mps0);
    e_str(line, &n, lim, " ncfg=");
    e_dec(line, &n, lim, (uint32_t)d->ncfg);
    e_emit(line, n, lim);
}

/* 配置描述符打印行 */
static void e_log_cfg(const usb_dev_t *d) {
    char line[128];
    int n = 0;
    int lim = (int)sizeof(line) - 1;
    e_str(line, &n, lim, "USB-ENUM: cfg value=");
    e_dec(line, &n, lim, (uint32_t)d->cfg_value);
    e_str(line, &n, lim, " total=");
    e_dec(line, &n, lim, (uint32_t)d->cfg_total);
    e_str(line, &n, lim, " nif=");
    e_dec(line, &n, lim, (uint32_t)d->nif);
    e_emit(line, n, lim);
}

/* 接口打印行：USB-ENUM: if<n> cls=03 sub=01 proto=01 hid */
static void e_log_if(int ifnum, uint8_t cls, uint8_t sub, uint8_t proto, int is_hid) {
    char line[128];
    int n = 0;
    int lim = (int)sizeof(line) - 1;
    e_str(line, &n, lim, "USB-ENUM: if");
    e_dec(line, &n, lim, (uint32_t)ifnum);
    e_str(line, &n, lim, " cls=");
    e_hex(line, &n, lim, (uint32_t)cls, 2);
    e_str(line, &n, lim, " sub=");
    e_hex(line, &n, lim, (uint32_t)sub, 2);
    e_str(line, &n, lim, " proto=");
    e_hex(line, &n, lim, (uint32_t)proto, 2);
    if (is_hid) e_str(line, &n, lim, " HID");
    e_emit(line, n, lim);
}

/* 端点打印行：USB-ENUM: if<n> ep=81 attr=03 mps=8 int=10 */
static void e_log_ep(int ifnum, uint8_t ep, uint8_t attr, uint16_t mps, uint8_t iv) {
    char line[128];
    int n = 0;
    int lim = (int)sizeof(line) - 1;
    e_str(line, &n, lim, "USB-ENUM: if");
    e_dec(line, &n, lim, (uint32_t)ifnum);
    e_str(line, &n, lim, " ep=");
    e_hex(line, &n, lim, (uint32_t)ep, 2);
    e_str(line, &n, lim, " attr=");
    e_hex(line, &n, lim, (uint32_t)attr, 2);
    e_str(line, &n, lim, " mps=");
    e_dec(line, &n, lim, (uint32_t)mps);
    e_str(line, &n, lim, " int=");
    e_dec(line, &n, lim, (uint32_t)iv);
    e_emit(line, n, lim);
}

/* HID 描述符打印行 */
static void e_log_hid(uint16_t bcd, uint8_t country, uint16_t rlen) {
    char line[128];
    int n = 0;
    int lim = (int)sizeof(line) - 1;
    e_str(line, &n, lim, "USB-ENUM: hid bcd=0x");
    e_hex(line, &n, lim, (uint32_t)bcd, 4);
    e_str(line, &n, lim, " country=");
    e_dec(line, &n, lim, (uint32_t)country);
    e_str(line, &n, lim, " rlen=");
    e_dec(line, &n, lim, (uint32_t)rlen);
    e_emit(line, n, lim);
}

/*
 * 解析配置描述符：顺序扫描 bLength/bDescriptorType 链表。
 * 只认三类：配置(0x02)、接口(0x04)、端点(0x05)。
 * 取第一个 HID 接口（class 0x03）及其第一个中断 IN 端点。
 */
static void parse_config(const uint8_t *c, int n, usb_dev_t *d) {
    int off = 0;
    int cur_if = -1;
    usb_hid_if_t *hid=0;
    while (off + 2 <= n) {
        uint8_t len  = c[off];
        uint8_t type = c[off + 1];
        if (len < 2) break;              /* 防死循环 / 畸形描述符 */
        if (off + len > n) break;        /* 越界即停，不读半截 */
        if (type == 0x04 && len >= 9) {
            cur_if = (int)c[off + 2];
            uint8_t cls   = c[off + 5];
            uint8_t sub   = c[off + 6];
            uint8_t proto = c[off + 7];
            /* SET_CONFIGURATION selects alternate setting zero. Endpoints
             * from an inactive alternate interface must not be claimed. */
            hid=0;
            if(c[off+3]!=0){cur_if=-1;off+=len;continue;}
            int is_hid=cls==3;
            if(is_hid && d->hid_count<USB_INPUT_INTERFACES) {
                hid=&d->hid[d->hid_count++];
                hid->number=(uint8_t)cur_if;
                hid->sub=sub;
                hid->proto=proto;
            }
            /* H1c：Mass Storage 接口（class 0x08）同样只记第一个 */
            if (cls == 0x08u && d->msc_if < 0) {
                d->msc_if    = cur_if;
                d->msc_sub   = sub;
                d->msc_proto = proto;
            }
            e_log_if(cur_if, cls, sub, proto, is_hid);
        } else if (type == 0x05 && len >= 7 && cur_if >= 0) {
            uint8_t  ep   = c[off + 2];
            uint8_t  attr = c[off + 3];
            uint16_t mps  = (uint16_t)(c[off + 4] | (c[off + 5] << 8));
            uint8_t  iv   = c[off + 6];
            e_log_ep(cur_if, ep, attr, mps, iv);
            /* 中断 IN（bit7=1 且 attr&3==3）才给 HID 用；只取第一个 */
            if(hid && !hid->ep && ep>=0x81 && ep<=0x8f &&
               (attr&3u)==3 && iv && (mps&2047u) && (mps&2047u)<=64) {
                hid->ep=ep;
                hid->mps=mps&2047u;
                hid->interval=iv;
            }
            /* bulk 端点（attr&3==2）给 MSC 用：IN/OUT 各记第一个 */
            if (cur_if == d->msc_if && (attr & 0x03u) == 0x02u) {
                if ((ep & 0x80u) != 0u && d->msc_ep_in == 0u) {
                    d->msc_ep_in     = ep;
                    d->msc_ep_in_mps = mps;
                } else if ((ep & 0x80u) == 0u && d->msc_ep_out == 0u) {
                    d->msc_ep_out     = ep;
                    d->msc_ep_out_mps = mps;
                }
            }
        } else if(type==0x21 && len>=9 && hid) {
            unsigned count=c[off+5];
            for(unsigned j=0;j<count && 8+3*j<len;j++)
                if(c[off+6+3*j]==0x22) {
                    hid->report_length=c[off+7+3*j]|((uint16_t)c[off+8+3*j]<<8);
                    break;
                }
            e_log_hid((uint16_t)(c[off+2]|(c[off+3]<<8)),c[off+4],hid->report_length);
        }
        off += len;
    }
}

/* 单个端口的完整枚举。成功返回 0 并填 *out。 */
static int enum_one(const usbhc_t *bus, uint8_t addr, usb_dev_t *out) {
    int port = (int)bus->port;
    int ls   = (int)bus->ls;
    uint8_t buf[1024];
    unsigned limit=(bus->hc==USBHC_XHCI || bus->hc==USBHC_EHCI) ? sizeof(buf) : 255u;
    int act = 0;

    for (int i = 0; i < (int)sizeof(buf); i++) buf[i] = 0;
    /* A failed port may have partly filled the same destination slot. */
    *out=(usb_dev_t){0};
    out->bus      = *bus;
    out->port     = (uint8_t)port;
    out->addr     = addr;
    out->lowspeed = (uint8_t)(ls ? 1 : 0);
    out->msc_if   = -1;

    /* 0) 端口复位：设备回到默认态（地址 0）。未连设备直接 fail closed 返回。 */
    if (!usbhc_port_reset(bus)) {
        e_fail(port, "port reset", 0);
        return -1;
    }
    /* GRESET/端口复位后 HC 的 CF/RS 可能已被清掉，传输前必须重新起调度 */
    if (usbhc_hc_start(bus) != 0) {
        e_fail(port, "hc start", 0);
        return -1;
    }

    /* 1) 设备描述符前 8 字节：只为拿 bMaxPacketSize0（低速恒为 8） */
    {
        uint8_t r8[8] = {0x80, 0x06, 0x00, 0x01, 0x00, 0x00, 0x08, 0x00};
        act = 0;
        if (usbhc_control_xfer(bus, 0, 0, r8, 1, buf, 8, &act) != 0 || act < 8) {
            e_fail(port, "GET_DESCRIPTOR(dev,8)", act);
            return -1;
        }
        out->mps0 = buf[7];
        if (out->mps0 < 8u) out->mps0 = 8u;      /* 规范最小 8；异常值夹紧 */
        if (out->mps0 > 64u) out->mps0 = 64u;    /* 端点 0 合法上界 */
    }

    /* 2) 完整设备描述符（18 字节） */
    {
        uint8_t r18[8] = {0x80, 0x06, 0x00, 0x01, 0x00, 0x00, 18, 0x00};
        act = 0;
        if (usbhc_control_xfer(bus, 0, 0, r18, 1, buf, 18, &act) != 0 || act < 18) {
            e_fail(port, "GET_DESCRIPTOR(dev,18)", act);
            return -1;
        }
        out->vid   = (uint16_t)(buf[8]  | (buf[9]  << 8));
        out->pid   = (uint16_t)(buf[10] | (buf[11] << 8));
        out->cls   = buf[4];
        out->sub   = buf[5];
        out->proto = buf[6];
        out->ncfg  = buf[17];
        e_log_dev(port, out);
    }

    /* 3) SET_ADDRESS：之后按规范等 >=2ms 设备才真正切到新地址 */
    {
        uint8_t sa[8] = {0x00, 0x05, addr, 0x00, 0x00, 0x00, 0x00, 0x00};
        if (usbhc_control_xfer(bus, 0, 0, sa, 0, NULL, 0, NULL) != 0) {
            e_fail(port, "SET_ADDRESS", 0);
            return -1;
        }
        e_delay_ms(10);
    }

    /* 4) 用新地址重读设备描述符——地址确实生效的最直接证据 */
    {
        uint8_t r18[8] = {0x80, 0x06, 0x00, 0x01, 0x00, 0x00, 18, 0x00};
        uint8_t chk[32];
        act = 0;
        for (int i = 0; i < (int)sizeof(chk); i++) chk[i] = 0;
        if (usbhc_control_xfer(bus, addr, 0, r18, 1, chk, 18, &act) != 0 || act < 18) {
            e_fail(port, "GET_DESCRIPTOR(dev)@addr", act);
            return -1;
        }
        uint16_t vid = (uint16_t)(chk[8] | (chk[9] << 8));
        uint16_t pid = (uint16_t)(chk[10] | (chk[11] << 8));
        if (vid != out->vid || pid != out->pid) {
            e_fail(port, "addr verify", act);
            return -1;
        }
    }

    /* 5) 配置描述符前 9 字节：拿 wTotalLength / bConfigurationValue / 接口数 */
    uint16_t total = 9;
    {
        uint8_t c9[8] = {0x80, 0x06, 0x00, 0x02, 0x00, 0x00, 0x09, 0x00};
        uint8_t c[16];
        act = 0;
        for (int i = 0; i < (int)sizeof(c); i++) c[i] = 0;
        if (usbhc_control_xfer(bus, addr, 0, c9, 1, c, 9, &act) != 0 || act < 9) {
            e_fail(port, "GET_DESCRIPTOR(cfg,9)", act);
            return -1;
        }
        total = (uint16_t)(c[2] | (c[3] << 8));
        out->cfg_value = c[5];
        out->nif       = c[4];
        /* 不可信上界：wTotalLength 至少 9（配置头自身），至多取 sizeof(buf) */
        if (total < 9u) total = 9u;
        if (total > limit) total = (uint16_t)limit;
    }

    /* 6) 完整配置描述符并解析接口/端点 */
    {
        uint8_t cf[8] = {0x80, 0x06, 0x00, 0x02, 0x00, 0x00,
                         (uint8_t)(total & 0xFFu), (uint8_t)((total >> 8) & 0xFFu)};
        act = 0;
        if (usbhc_control_xfer(bus, addr, 0, cf, 1, buf, (int)total, &act) != 0 || act < 9) {
            e_fail(port, "GET_DESCRIPTOR(cfg,full)", act);
            return -1;
        }
        out->cfg_total = (uint16_t)act;
        e_log_cfg(out);
        parse_config(buf, act, out);
    }

    /* 7) SET_CONFIGURATION：让设备进入配置态（端点可用） */
    {
        uint8_t sc[8] = {0x00, 0x09, out->cfg_value, 0x00, 0x00, 0x00, 0x00, 0x00};
        if (usbhc_control_xfer(bus, addr, 0, sc, 0, NULL, 0, NULL) != 0) {
            e_fail(port, "SET_CONFIGURATION", 0);
            return -1;
        }
        out->configured = 1;
        dmesg_write("USB-ENUM: SET_CONFIGURATION ok");
    }

    /* Read one report descriptor per interface. Ignore consumer-control
     * and vendor interfaces: don't let one hide the receiver's real inputs. */
    int kbd=0,pointer=0;
    unsigned used_ep=0;
    for(unsigned j=0;j<out->hid_count;j++) {
        usb_hid_if_t *h=&out->hid[j];
        if(!h->ep)continue;
        if(h->report_length && h->report_length<=limit) {
            unsigned want=h->report_length;
            uint8_t rr[]={0x81,6,0,0x22,h->number,0,(uint8_t)want,(uint8_t)(want>>8)};
            act=0;
            if(usbhc_control_xfer(bus,addr,0,rr,1,buf,(int)want,&act)==0 && (unsigned)act==want) {
                unsigned id=0;
                const int mode=hid_pointer_layout_id(buf,want,&id);
                if(mode && h->mps>=hid_pointer_bytes(mode)+!!id){
                    h->pointer_layout=mode;
                    h->pointer_id=(uint8_t)id;
                }
                hid_key_bitmap_t bitmap=hid_key_bitmap(buf,want);
                if(bitmap.bytes && bitmap.bytes<=h->mps &&
                   !(h->sub==1 && h->proto!=1))h->bitmap=bitmap;
                if(!h->bitmap.bytes && h->sub!=1 && h->mps>=8) {
                    unsigned key=hid_key_layout(buf,want);
                    if(key==HID_KEY_NO_ID || h->mps>=9)h->key_layout=key;
                }
                char line[96];int n=0,lim=(int)sizeof(line)-1;
                e_str(line,&n,lim,"USB-ENUM: hid report ok len=");
                e_dec(line,&n,lim,(unsigned)act);
                e_emit(line,n,lim);
            } else e_fail(port,"GET_DESCRIPTOR(report)",act);
        }
        int key=usb_hid_keyboard(h), ptr=usb_hid_pointer(h);
        unsigned ep_bit=1u<<(h->ep&15u);
        if((!key || kbd) && (!ptr || pointer))continue;
        if(used_ep&ep_bit)continue;
        if(usbhc_configure_hid(bus,addr,h->ep,h->mps,h->interval)!=0) {
            e_fail(port,"configure HID interrupt endpoint",0);
            continue;
        }
        h->ready=1;
        used_ep|=ep_bit;
        kbd|=key;pointer|=ptr;
    }

    return 0;
}

static void boot_hubs(void);

/* ---------- 入口 ---------- */
void usbenum_init(void) {
    int nports = usbhc_port_count();

    g_dev_n = 0;
    for (int i = 0; i < USBENUM_MAX_DEV; i++) {
        usb_dev_t *d = &g_dev[i];
        d->bus.hc = 0; d->bus.ctl = 0; d->bus.port = 0; d->bus.ls = 0;
        d->bus.io = 0; d->port = 0; d->addr = 0; d->lowspeed = 0;
        d->vid = 0; d->pid = 0; d->cls = 0; d->sub = 0; d->proto = 0;
        d->mps0 = 0; d->ncfg = 0; d->cfg_value = 0; d->cfg_total = 0;
        d->nif = 0; d->hid_count = 0; d->configured = 0;
        d->msc_if = -1; d->msc_sub = 0; d->msc_proto = 0;
        d->msc_ep_in = 0; d->msc_ep_in_mps = 0;
        d->msc_ep_out = 0; d->msc_ep_out_mps = 0;
    }

    if (nports <= 0) {
        dmesg_write("USB-ENUM: no connected port, skip");
    } else {
        char line[128];
        int n = 0;
        int lim = (int)sizeof(line) - 1;
        e_str(line, &n, lim, "USB-ENUM: begin ports=");
        e_dec(line, &n, lim, (uint32_t)nports);
        e_emit(line, n, lim);

        for (int i = 0; i < nports && g_dev_n < USBENUM_MAX_DEV; i++) {
            usbhc_t bus;
            if (usbhc_port_get(i, &bus) != 0) continue;
            uint8_t addr = (uint8_t)(g_dev_n + 1);   /* 地址 1..127 顺序分配 */
            if (enum_one(&bus, addr, &g_dev[g_dev_n]) == 0)
                g_dev_n++;
            else if(bus.hc==USBHC_EHCI)
                ehci_root_disable(bus.ctl,bus.port);
        }
    }

    boot_hubs();
    {
        char line[128];
        int n = 0;
        int lim = (int)sizeof(line) - 1;
        /* 前缀必须带 "USB-ENUM:"：E2E 按前缀收集诊断行（别处也可能出现
         * "device(s) enumerated" 字样，没有前缀就会收集错行）。 */
        e_str(line, &n, lim, "USB-ENUM: ");
        e_dec(line, &n, lim, (uint32_t)g_dev_n);
        e_str(line, &n, lim, " device(s) enumerated");
        e_emit(line, n, lim);
    }
}

int usbenum_device_count(void) {
    return g_dev_n;
}

const usb_dev_t *usbenum_get(int i) {
    if (i < 0 || i >= g_dev_n || !g_dev[i].addr) return NULL;
    return &g_dev[i];
}

/* One task owns all post-boot xHCI commands and endpoint-zero requests.
 * Enumerated boot storage on other controllers keeps its original address. */
static wait_queue_t input_wq={-1};
static uint8_t settled[XHCI_MAX_CTL][XHCI_MAX_ROOT],attempted[XHCI_MAX_CTL][XHCI_MAX_ROOT];
static uint8_t uhci_seen[UHCI_MAX_CTL][2], uhci_stable[UHCI_MAX_CTL][2];

/* Hub relationships use enumeration-table slots,
 * not USB addresses. No child descriptor or hub power state touches storage. */
typedef struct {
    uint8_t count, parent, down;
    uint16_t attempted, stable;
} hub_meta;
static hub_meta hubs[USBENUM_MAX_DEV];

static int input_device(const usb_dev_t *d) {
    for(unsigned i=0;i<d->hid_count;i++)if(d->hid[i].ready)return 1;
    return 0;
}

static int free_entry(void) {
    int i=0;
    while(i<g_dev_n && g_dev[i].addr)i++;
    return i==USBENUM_MAX_DEV ? -1 : i;
}

static void publish(int i,const usb_dev_t *d,hub_meta m) {
    hubs[i]=m;
    g_dev[i]=*d;
    if(i==g_dev_n)g_dev_n++;
}

static void retire(int i) {
    usb_dev_t *d=&g_dev[i];
    usbkbd_detach(d);
    usbmouse_detach(d);
    if(d->bus.hc==USBHC_XHCI)xhci_port_stop(d->bus.ctl,d->bus.port);
    if(d->bus.hc==USBHC_UHCI)uhci_release_hid(d->bus.io,d->addr);
    if(d->bus.hc==USBHC_OHCI)ohci_release_hid(d->bus.ctl&127u,d->addr);
    if(d->bus.hc==USBHC_EHCI)ehci_release_hid(d->bus.ctl,d->addr);
    d->addr=d->configured=0;
    hubs[i]=(hub_meta){0};
}

static void retire_tree(int i) {
    for(int child=0;child<g_dev_n;child++)
        if(g_dev[child].addr && hubs[child].parent==i+1)retire_tree(child);
    retire(i);
}

static int port_feature(const usb_dev_t *hub,unsigned port,int set,unsigned feature) {
    uint8_t req[]={0x23,(uint8_t)(set?3:1),(uint8_t)feature,0,(uint8_t)port,0,0,0};
    return usbhc_control_xfer(&hub->bus,hub->addr,0,req,0,NULL,0,NULL);
}

static int port_status(const usb_dev_t *hub,unsigned port,unsigned *changes) {
    uint8_t req[]={0xa3,0,0,0,(uint8_t)port,0,4,0},buf[4];
    int size=0;
    if(usbhc_control_xfer(&hub->bus,hub->addr,0,req,1,buf,4,&size)!=0||size!=4)
        return -1;
    *changes=buf[2]|((unsigned)buf[3]<<8);
    return buf[0]|((unsigned)buf[1]<<8);
}

static unsigned configure_hub(const usb_dev_t *hub) {
    int ss=hub->bus.hc==USBHC_XHCI && xhci_device_speed(hub->bus.ctl,hub->bus.port)==4;
    uint8_t desc=ss?0x2a:0x29;
    uint8_t req[]={0xa0,6,0,desc,0,0,(uint8_t)(ss?12:9),0},buf[12];
    int size=0;
    if(hub->cls!=9 ||
       (hub->bus.hc==USBHC_EHCI && (hub->bus.hub_port ||
                                   (hub->proto!=1 && hub->proto!=2))) ||
       usbhc_control_xfer(&hub->bus,hub->addr,0,req,1,buf,req[6],&size)!=0 ||
       size<(ss?12:7) || buf[1]!=desc || !buf[2] || buf[2]>15)return 0;
    unsigned count=buf[2];
    if(hub->bus.hc==USBHC_XHCI && xhci_hub_configure(hub->bus.ctl,hub->bus.port,count,(buf[3]>>5)&3u)!=0)return 0;
    if(ss){
        uint8_t depth[]={0x20,12,(uint8_t)xhci_hub_depth(hub->bus.ctl,hub->bus.port),0,0,0,0,0};
        if(usbhc_control_xfer(&hub->bus,hub->addr,0,depth,0,0,0,0)!=0)return 0;
    }
    if(hub->bus.hc!=USBHC_EHCI || (buf[3]&3u)<2u)
        for(unsigned p=1;p<=count;p++)
            if(port_feature(hub,p,1,8)!=0)return 0; /* PORT_POWER */
    unsigned delay=(unsigned)buf[5]*2u;
    e_delay_ms(delay<20 ? 20 : delay);
    dmesg_write(ss?"USB-HID: SuperSpeed hub ready":"USB-HID: USB 2.0 hub ready");
    return count;
}

static int poll_hub(int entry) {
    hub_meta *h=&hubs[entry];
    const usb_dev_t *hub=&g_dev[entry];
    int ss=hub->bus.hc==USBHC_XHCI && xhci_device_speed(hub->bus.ctl,hub->bus.port)==4;
    int changed=0;
    for(unsigned p=1;p<=h->count;p++) {
        unsigned changes,mask=1u<<p;
        int status=port_status(hub,p,&changes);
        if(status<0){
            if(hub->bus.hc==USBHC_EHCI)break; /* May have lost the whole hub. */
            continue;
        }
        for(unsigned bit=0;bit<5;bit++)
            if(changes&(1u<<bit))(void)port_feature(hub,p,0,16+bit);
        if(ss){
            if(changes&0x20)(void)port_feature(hub,p,0,29); /* C_BH_PORT_RESET */
            if(changes&0x40)(void)port_feature(hub,p,0,25); /* C_PORT_LINK_STATE */
            if(changes&0x80)(void)port_feature(hub,p,0,26); /* C_PORT_CONFIG_ERROR */
        }
        if(!(status&1) || (changes&1u) || ((changes&2u) && !(status&2))) {
            h->stable&=~mask;h->attempted&=~mask;
            for(int i=0;i<g_dev_n;i++)
                if(g_dev[i].addr && hubs[i].parent==entry+1 && hubs[i].down==p) {
                    retire_tree(i);changed=1;
                    dmesg_write("USB-HID: detached via hub");
                }
        }
        if(!(status&1) || (status&8) || (h->attempted&mask))continue;
        if(!(h->stable&mask)){h->stable|=mask;continue;}
        int i=free_entry();
        if(i<0)continue;
        h->attempted|=mask;
        unsigned reset=ss && (status&0x01e0)?28u:4u;
        if(port_feature(hub,p,1,reset)!=0)continue;
        int enabled=0;
        for(unsigned round=0;round<12;round++){
            e_delay_ms(10);
            status=port_status(hub,p,&changes);
            if(status<0 || !(status&1))break;
            if((status&0x12)==2 && (!ss || !(status&0x01e0))){enabled=1;break;}
        }
        if(!enabled)continue;
        (void)port_feature(hub,p,0,reset==28?29u:20u);
        unsigned speed=ss?4u:(status&512)?2u:(status&1024)?3u:1u;
        int handle=-1;
        usbhc_t bus=hub->bus;
        if(bus.hc==USBHC_XHCI) {
            handle=xhci_hub_child(hub->bus.ctl,hub->bus.port,p,speed);
            if(handle<0)continue;
            bus.port=(uint8_t)handle;
        } else if(bus.hc==USBHC_EHCI) {
            bus.hub_port=(uint8_t)p;
            bus.tt_hub=speed==3 ? 0 : hub->addr;
            bus.ls=speed==2;
        } else {
            /* This downstream port has already been reset by the hub. */
            bus.ctl=bus.hc==USBHC_OHCI ? bus.ctl|USBHC_DOWNSTREAM : 1;
            bus.ls=speed==2;
        }
        usb_dev_t d;
        if(enum_one(&bus,(uint8_t)(i+1),&d)==0) {
            unsigned ports=handle>=0?configure_hub(&d):0;
            if(ports || input_device(&d)){
                publish(i,&d,(hub_meta){.count=(uint8_t)ports,.parent=(uint8_t)(entry+1),.down=(uint8_t)p});
                changed=1;
                dmesg_write("USB-HID: attached via hub");
                continue;
            }
        }
        if(bus.hc==USBHC_OHCI)ohci_release_hid(bus.ctl&127u,(uint8_t)(i+1));
        if(bus.hc==USBHC_EHCI)ehci_release_hid(bus.ctl,(uint8_t)(i+1));
        if(handle>=0)xhci_port_stop(bus.ctl,(unsigned)handle);
        else (void)port_feature(hub,p,0,1);
    }
    return changed;
}

static void boot_hubs(void) {
    int roots=g_dev_n;
    for(int i=0;i<roots;i++) {
        unsigned count=configure_hub(&g_dev[i]);
        if(!count)continue;
        hubs[i].count=(uint8_t)count;
    }
    for(unsigned round=0;round<=2u*XHCI_HUB_TIERS;round++){
        int pending=0;
        for(int i=0;i<g_dev_n;i++)if(hubs[i].count){
            (void)poll_hub(i);
            pending|=!!(hubs[i].stable&~hubs[i].attempted);
        }
        if(!pending)break;
        e_delay_ms(100);
    }
}

static int poll_uhci_root(unsigned ctl) {
    uint16_t io=uhci_root_io(ctl);
    if(!io)return 0;
    int changed=0;
    for(unsigned p=0;p<2;p++) {
        unsigned status=uhci_root_status(io,p);
        int prev=-1;
        for(int i=0;i<g_dev_n;i++) {
            usb_dev_t *d=&g_dev[i];
            if(d->addr && d->bus.hc==USBHC_UHCI && d->bus.io==io && d->bus.port==p &&
               !hubs[i].parent) {prev=i;break;}
        }
        if(!(status&1u) || (status&2u)) {
            uhci_stable[ctl][p]=uhci_seen[ctl][p]=0;
            if(prev>=0) {
                /* Never reuse the object for a boot mass-storage drive. */
                if(g_dev[prev].msc_if>=0)continue;
                retire_tree(prev);
                prev=-1;
                changed=1;
                dmesg_write("USB-HID: detached UHCI root port");
            }
        }
        if(prev>=0 || !(status&1u) || uhci_seen[ctl][p])continue;
        if(uhci_stable[ctl][p]<2){uhci_stable[ctl][p]++;continue;}
        int i=free_entry();
        if(i<0)continue;
        uhci_seen[ctl][p]=1;
        usbhc_t bus={.hc=USBHC_UHCI,.port=(uint8_t)p,.ls=!!(status&4u),.io=io};
        usb_dev_t d;
        if(enum_one(&bus,(uint8_t)(i+1),&d)==0) {
            unsigned ports=configure_hub(&d);
            if(ports || input_device(&d)) {
                publish(i,&d,(hub_meta){.count=(uint8_t)ports});
                changed=1;
                dmesg_write("USB-HID: attached UHCI root port");
                continue;
            }
        }
        uhci_release_hid(io,(uint8_t)(i+1));
        uhci_root_disable(io,p);
    }
    return changed;
}

static uint8_t ohci_seen[OHCI_MAX_CTL][OHCI_MAX_ROOT];
static uint8_t ohci_stable[OHCI_MAX_CTL][OHCI_MAX_ROOT];

static int poll_ohci_roots(void) {
    int changed=0;
    for(unsigned ctl=0;ctl<OHCI_MAX_CTL;ctl++){
        for(unsigned p=0;p<ohci_root_count(ctl);p++){
            unsigned status=ohci_root_status(ctl,p,1);
            int prev=-1;
            for(int i=0;i<g_dev_n;i++){
                usb_dev_t *d=&g_dev[i];
                if(d->addr && d->bus.hc==USBHC_OHCI && d->bus.ctl==ctl &&
                   d->bus.port==p && !hubs[i].parent){prev=i;break;}
            }
            if(!(status&1u) || (status&2u)){
                ohci_seen[ctl][p]=ohci_stable[ctl][p]=0;
                if(prev>=0){
                    retire_tree(prev);
                    prev=-1;
                    changed=1;
                    dmesg_write("USB-HID: detached OHCI root port");
                }
            }
            if(prev>=0 || !(status&1u) || ohci_seen[ctl][p])continue;
            if(ohci_stable[ctl][p]<2){ohci_stable[ctl][p]++;continue;}
            int i=free_entry();
            if(i<0)continue;
            ohci_seen[ctl][p]=1;
            usbhc_t bus={.hc=USBHC_OHCI,.ctl=(uint8_t)ctl,.port=(uint8_t)p,.ls=!!(status&4u)};
            usb_dev_t d;
            if(enum_one(&bus,(uint8_t)(i+1),&d)==0){
                unsigned ports=configure_hub(&d);
                if(ports || input_device(&d)){
                    publish(i,&d,(hub_meta){.count=(uint8_t)ports});
                    changed=1;
                    dmesg_write("USB-HID: attached OHCI root port");
                    continue;
                }
            }
            ohci_release_hid(ctl,(uint8_t)(i+1));
            ohci_root_disable(ctl,p);
        }
    }
    return changed;
}

static int poll_ehci_roots(void) {
    static uint8_t phase[EHCI_MAX_CTL][EHCI_MAX_PORTS];
    int changed=0, probed=0;
    for(int c=0;c<EHCI_MAX_CTL;c++){
        for(unsigned p=0;p<ehci_root_count(c);p++){
            int prev=-1;
            for(int i=0;i<g_dev_n;i++){
                usb_dev_t *dev=&g_dev[i];
                if(dev->addr && !hubs[i].parent && dev->bus.hc==USBHC_EHCI && dev->bus.ctl==c &&
                   dev->bus.port==p){prev=i;break;}
            }
            /* Leave mounted drives and their device/address handles alone. */
            if(prev>=0 && g_dev[prev].msc_if>=0)continue;
            unsigned status=ehci_root_status(c,(int)p);
            if(!(status&1u) || (status&2u)){
                phase[c][p]=0;
                if(prev>=0){
                    retire_tree(prev);
                    prev=-1;
                    changed=1;
                    dmesg_write("USB-HID: detached EHCI root port");
                }
            }
            if(prev>=0 || !(status&1u) || (status&4u) || phase[c][p]>=3)continue;
            if(phase[c][p]<2){phase[c][p]++;continue;}
            if(probed)continue; /* One blocking reset on each worker pass. */
            int i=free_entry();
            if(i<0)continue;
            phase[c][p]=3;
            probed=1;
            usbhc_t bus={.hc=USBHC_EHCI,.ctl=(uint8_t)c,.port=(uint8_t)p};
            usb_dev_t d;
            if(enum_one(&bus,(uint8_t)(i+1),&d)==0 && d.msc_if<0){
                unsigned ports=configure_hub(&d);
                if(ports || input_device(&d)){
                    publish(i,&d,(hub_meta){.count=(uint8_t)ports});
                    changed=1;
                    dmesg_write("USB-HID: attached EHCI root port");
                    continue;
                }
            }
            ehci_release_hid(c,(uint8_t)(i+1));
            ehci_root_disable(c,(int)p);
        }
    }
    return changed;
}

static void input_service(void *unused) {
    (void)unused;
    for (;;) {
        int changed=poll_ehci_roots();
        for(unsigned c=0;c<UHCI_MAX_CTL;c++)changed|=poll_uhci_root(c);
        changed|=poll_ohci_roots();
        for(unsigned ctl=0;ctl<XHCI_MAX_CTL;ctl++)
        for(unsigned p=0;p<xhci_root_ports(ctl);p++) {
            unsigned status=xhci_root_status(ctl,p);
            usb_dev_t *previous=NULL;
            for(int i=0;i<g_dev_n;i++) {
                usb_dev_t *d=&g_dev[i];
                if(d->addr && d->bus.hc==USBHC_XHCI && d->bus.ctl==ctl && d->bus.port==p){previous=d;break;}
            }
            if(!(status&1u) || (status&2u)) {
                settled[ctl][p]=attempted[ctl][p]=0;
                if(previous) {
                    int parent=(int)(previous-g_dev);
                    retire_tree(parent);
                    changed=1;
                    dmesg_write("USB-HID: detached xHCI root port");
                    previous=NULL;
                }
            }
            if(previous || !(status&1u) || attempted[ctl][p]) continue;
            if(settled[ctl][p]<2){settled[ctl][p]++;continue;}
            int i=free_entry();
            if(i<0)continue;
            attempted[ctl][p]=1;
            usbhc_t bus={.hc=USBHC_XHCI,.ctl=(uint8_t)ctl,.port=(uint8_t)p};
            /* Keep the table unpublished while a device is being probed. */
            usb_dev_t candidate;
            if(enum_one(&bus,(uint8_t)(i+1),&candidate)==0) {
                unsigned ports=configure_hub(&candidate);
                if(ports || input_device(&candidate)) {
                    publish(i,&candidate,(hub_meta){.count=(uint8_t)ports});
                    changed=1;
                    dmesg_write("USB-HID: attached xHCI root port");
                    continue;
                }
            }
            xhci_port_stop(ctl,p);
        }
        for(int i=0;i<g_dev_n;i++) {
            if(g_dev[i].addr && g_dev[i].cls==9 && !hubs[i].parent && !hubs[i].count) {
                unsigned ports=configure_hub(&g_dev[i]);
                if(ports)hubs[i].count=(uint8_t)ports;
                else g_dev[i].cls=0; /* unsupported hub; do not keep retrying */
            }
            if(hubs[i].count)changed|=poll_hub(i);
        }
        if(changed) {
            usbkbd_init();
            usbmouse_init();
        }
        usbkbd_sync_leds();
        uint32_t flags=irq_save_disable();
        task_sleep(&input_wq,75);
        irq_restore(flags);
    }
}

void usbenum_start_input_service(void) {
    if (!xhci_root_ports(0) && !uhci_root_io(0) && !ohci_root_count(0) &&
        !ehci_root_count(0) && !usbkbd_present())return;
    for(int i=0;i<g_dev_n;i++) {
        usb_dev_t *d=&g_dev[i];
        if(d->bus.hc==USBHC_XHCI && d->bus.ctl<XHCI_MAX_CTL && d->bus.port<XHCI_MAX_ROOT && !hubs[i].parent)
            attempted[d->bus.ctl][d->bus.port]=1;
    }
    uint32_t flags=irq_save_disable();
    int pid=task_create("usb-hid",input_service,0);
    irq_restore(flags);
    if (pid<0)dmesg_write("USB-HID: cannot start hotplug worker");
}
