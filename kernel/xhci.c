/* xHCI 1.x polling driver for HID on root ports and cascaded hubs.
 * DMA uses kernel-owned rings and bounded bounce buffers, never caller
 * addresses. The input service handles post-boot attach/detach.
 * No bulk, streams, or isochronous transport in this driver. */
#include "xhci.h"
#include "pci.h"
#include "paging.h"
#include "pmm.h"
#include "isr.h"
#include "irqflags.h"
#include "mem.h"
#include "dmesg.h"

#define ROOT_PORTS XHCI_MAX_ROOT
#define DEVICES 8u
#define SCRATCH_MAX 64u
#define RING_N 64u
#define EVENT_N 256u
#define TRB(t) ((uint32_t)(t) << 10)
#define CYCLE 1u
#define IOC (1u << 5)
#define ISP (1u << 2)
#define IDT (1u << 6)
#define INT_DISABLE (1u << 10)
#define PORTSC(n) (0x400u + (uint32_t)(n) * 16u)
#define PORT_RW ((1u << 9) | (3u << 14) | (7u << 25))
#define PORT_CHANGES (0x7fu << 17)

typedef struct { uint32_t lo, hi, status, ctl; } trb_t;
typedef struct {
    trb_t trb[RING_N] __attribute__((aligned(1024)));
    uint32_t next, cycle;
} ring_t;
#define XHCI_HID_ENDPOINTS 2u
typedef struct {
    ring_t ring;
    uint8_t buf[64] __attribute__((aligned(64)));
    uint8_t dci,mps,pending,done,code,failed;
    uint32_t address,residual;
} hid_ep_t;
typedef struct {
    uint32_t out[32 * 16] __attribute__((aligned(4096)));
    uint32_t in[33 * 16] __attribute__((aligned(4096)));
    ring_t ep0;
    hid_ep_t hid[XHCI_HID_ENDPOINTS];
    uint8_t ctrl_buf[1024] __attribute__((aligned(1024)));
    uint8_t slot,speed,mps0,root,is_hub,ctrl_dead,depth;
    uint32_t route,tt;
    uint32_t ctrl_status,ctrl_data,ctrl_code,ctrl_residual;
} device_t;

typedef struct {
    device_t devices[DEVICES];
    uint64_t dcbaa[256] __attribute__((aligned(64)));
    uint64_t scratch[SCRATCH_MAX] __attribute__((aligned(64)));
    trb_t events[EVENT_N] __attribute__((aligned(4096)));
    trb_t erst __attribute__((aligned(64)));
    ring_t command;
    const pci_device_t *pdev;
    volatile uint8_t *cap, *op, *rt, *db;
    uint32_t event_next, event_cycle, stride, cmd_addr, cmd_code, cmd_slot;
    uint8_t ports[ROOT_PORTS], port_device[ROOT_PORTS+DEVICES], slot_type[ROOT_PORTS];
    uint32_t slot_count, port_count;
    int connected, running, high_mmio;
    uint64_t mmio_base;
    uint32_t mmio_size;
} xhci_t;
static xhci_t hosts[XHCI_MAX_CTL];
static unsigned host_count;

static xhci_t *host(unsigned ctl) {
    return ctl<host_count ? &hosts[ctl] : 0;
}

static uint32_t addr(const void *p) { return (uint32_t)p; }
static uint32_t rd(xhci_t *hc,volatile uint8_t *p, uint32_t off) {
    if (hc->high_mmio) return paging_mmio64_read((uint32_t)p - PAGING_MMIO64_VA + off);
    return *(volatile uint32_t *)(p + off);
}
static void wr(xhci_t *hc,volatile uint8_t *p, uint32_t off, uint32_t v) {
    if (hc->high_mmio) {
        paging_mmio64_write((uint32_t)p - PAGING_MMIO64_VA + off, v);
        return;
    }
    *(volatile uint32_t *)(p + off) = v;
}
static void wr64(xhci_t *hc,volatile uint8_t *p, uint32_t off, uint32_t lo) {
    wr(hc,p, off, lo);
    wr(hc,p, off + 4, 0);
}
static void pci_command(xhci_t *hc,uint16_t cmd) {
    /* Writing zero to PCI status leaves its W1C bits alone. */
    pci_write_dword(hc->pdev->bus, hc->pdev->dev, hc->pdev->func, PCI_REG_COMMAND, cmd);
}
static void fence(void) { asm volatile("" ::: "memory"); }

static int wait_reg(xhci_t *hc,volatile uint8_t *p, uint32_t off, uint32_t mask,
                    uint32_t wanted, uint32_t timeout) {
    uint32_t start = g_pit_ticks;
    for (uint32_t i = 0; i < 20000000u; i++) {
        if ((rd(hc,p, off) & mask) == wanted) return 1;
        if ((uint32_t)(g_pit_ticks - start) >= timeout) break;
        asm volatile("pause");
    }
    return 0;
}

static void stop_controller(xhci_t *hc,const char *why) {
    hc->running = 0;
    hc->connected = 0;
    if (hc->op) wr(hc,hc->op, 0, rd(hc,hc->op, 0) & ~1u);
    uint16_t c = pci_read_word(hc->pdev->bus, hc->pdev->dev, hc->pdev->func, PCI_REG_COMMAND);
    pci_command(hc,(c & (uint16_t)~PCI_CMD_BUS_MASTER) | INT_DISABLE);
    dmesg_write(why);
}

static void ring_init(ring_t *r) {
    memset(r, 0, sizeof(*r));
    r->cycle = 1;
    r->trb[RING_N - 1].lo = addr(r->trb);
    r->trb[RING_N - 1].ctl = TRB(6) | (1u << 1) | CYCLE; /* Link + Toggle Cycle */
}

static uint32_t enqueue(ring_t *r, uint32_t lo, uint32_t hi, uint32_t len, uint32_t ctl) {
    trb_t *t = &r->trb[r->next++];
    t->lo = lo; t->hi = hi; t->status = len;
    fence();                       /* publish cycle last */
    *(volatile uint32_t *)&t->ctl = ctl | r->cycle;
    if (r->next == RING_N - 1) {
        *(volatile uint32_t *)&r->trb[r->next].ctl = TRB(6) | (1u << 1) | r->cycle;
        r->next = 0;
        r->cycle ^= 1u;
    }
    return addr(t);
}

static device_t *device_at(xhci_t *hc,int port) {
    if (!hc->running || port < 0 || (uint32_t)port >= sizeof(hc->port_device) || !hc->port_device[port])
        return 0;
    return &hc->devices[hc->port_device[port] - 1];
}

static void poll_events(xhci_t *hc) {
    /* Work is bounded even if a device keeps generating port events. */
    uint32_t seen = 0;
    for (; seen < EVENT_N; seen++) {
        volatile trb_t *e = &hc->events[hc->event_next];
        uint32_t ctl = e->ctl;
        if ((ctl & CYCLE) != hc->event_cycle) break;
        fence();
        uint32_t type = (ctl >> 10) & 0x3fu;
        uint32_t slot = ctl >> 24, dci = (ctl >> 16) & 31u;
        uint32_t ptr = e->lo, status = e->status, code = status >> 24;
        if (type == 33 && !e->hi && ptr == hc->cmd_addr) {
            hc->cmd_slot = slot;
            hc->cmd_code = code;
        } else if (type == 32 && !e->hi) {
            for (uint32_t i = 0; i < DEVICES; i++) {
                device_t *d = &hc->devices[i];
                if (!d->slot || d->slot != slot) continue;
                if (dci == 1 && d->ctrl_status) {
                    if (ptr == d->ctrl_data && code == 13) {
                        d->ctrl_residual = status & 0xffffffu;
                    } else if (ptr == d->ctrl_status || (code != 1 && code != 13)) {
                        d->ctrl_code = code;
                    }
                } else {
                    for(unsigned j=0;j<XHCI_HID_ENDPOINTS;j++){
                        hid_ep_t *h=&d->hid[j];
                        if(h->dci==dci && h->pending && ptr==h->address){
                            h->residual=status&0xffffffu;
                            h->code=(uint8_t)code;
                            h->done=1;
                            break;
                        }
                    }
                }
                break;
            }
        }
        if (++hc->event_next == EVENT_N) { hc->event_next = 0; hc->event_cycle ^= 1u; }
    }
    if (seen) wr64(hc,hc->rt,0x38,addr(&hc->events[hc->event_next])|8u); /* ERDP/EHB */
}

static int wait_event(xhci_t *hc,const uint32_t *code,int command_wait) {
    uint32_t start = g_pit_ticks;
    for (uint32_t i = 0; i < 20000000u; i++) {
        uint32_t flags=irq_save_disable();
        poll_events(hc);
        int done=*code!=0;
        irq_restore(flags);
        if (done) return 1;
        if ((uint32_t)(g_pit_ticks - start) >= 500u || (rd(hc,hc->op, 4) & 0x1001u)) break;
        asm volatile("pause");
    }
    if(command_wait || (rd(hc,hc->op,4)&0x1001u))
        stop_controller(hc,"xHCI: event timeout; controller disabled");
    return 0;
}

static int cmd(xhci_t *hc,uint32_t type_bits, uint32_t lo) {
    if (!hc->running) return -1;
    hc->cmd_code = 0;
    hc->cmd_addr = enqueue(&hc->command, lo, 0, 0, type_bits);
    fence();
    wr(hc,hc->db, 0, 0);
    if (!wait_event(hc,&hc->cmd_code,1) || hc->cmd_code != 1) return -1;
    return (int)hc->cmd_slot;
}

static uint32_t *ic(xhci_t *hc,device_t *d, uint32_t index) { return d->in + index * hc->stride; }
static uint32_t *oc(xhci_t *hc,device_t *d, uint32_t index) { return d->out + index * hc->stride; }

/* xHCI 1.2b sections 7.1/7.2. Don't reset a controller until firmware
 * acknowledges ownership. Disable its legacy SMI traps after the handoff. */
static int prepare_protocols(xhci_t *hc,uint32_t off,uint32_t bar_size) {
    for(unsigned i=0;off && i<64;i++) {
        if(off>bar_size-4)return 0;
        uint32_t reg=rd(hc,hc->cap,off);
        if(reg==0xffffffffu)return 0;
        switch(reg&255u) {
        case 1: {
            if(off>bar_size-8)return 0;
            const uint32_t own=1u<<24, bios=1u<<16;
            if(!(reg&own))wr(hc,hc->cap,off,reg|own);
            if(!wait_reg(hc,hc->cap,off,own|bios,own,1000))return 0;
            const uint32_t enables=1u|(1u<<4)|(7u<<13), status=7u<<29;
            uint32_t legacy=rd(hc,hc->cap,off+4);
            wr(hc,hc->cap,off+4,(legacy&~enables)|status);
            break;
        }
        case 2: {
            if(off>bar_size-16)return 0;
            if(rd(hc,hc->cap,off+4)!=0x20425355u)break; /* "USB " */
            uint32_t range=rd(hc,hc->cap,off+8);
            unsigned first=range&255u,count=(range>>8)&255u;
            unsigned type=rd(hc,hc->cap,off+12)&31u;
            if(!first || !count || first+count-1>255)break;
            for(unsigned p=first-1;p<first-1+count && p<hc->port_count;p++)
                hc->slot_type[p]=(uint8_t)type;
            break;
        }
        default:break;
        }
        uint32_t step=((reg>>8)&255u)*4;
        if(!step)return 1;
        off+=step;
    }
    return off==0;
}

static int map_pci(xhci_t *hc,const pci_device_t *p) {
    uint32_t bar = p->bar[0], is64 = (bar & 6u) == 4u;
    uint64_t base = (bar & ~15u) | (is64 ? (uint64_t)p->bar[1] << 32 : 0);
    if ((bar & 1u) || (bar & 6u) == 2u || base < PAGING_IDENTITY_END) return 0;
    uint16_t c = pci_read_word(p->bus, p->dev, p->func, PCI_REG_COMMAND);
    pci_command(hc,(c & (uint16_t)~(PCI_CMD_MEM_SPACE | PCI_CMD_BUS_MASTER)) | INT_DISABLE);
    pci_write_dword(p->bus, p->dev, p->func, PCI_REG_BAR0, 0xffffffffu);
    if (is64) pci_write_dword(p->bus, p->dev, p->func, PCI_REG_BAR0 + 4, 0xffffffffu);
    uint32_t mask = pci_read_dword(p->bus, p->dev, p->func, PCI_REG_BAR0);
    uint32_t high = is64 ? pci_read_dword(p->bus, p->dev, p->func, PCI_REG_BAR0 + 4) : ~0u;
    pci_write_dword(p->bus, p->dev, p->func, PCI_REG_BAR0, bar);
    if (is64) pci_write_dword(p->bus, p->dev, p->func, PCI_REG_BAR0 + 4, p->bar[1]);
    uint32_t size = ~(mask & ~15u) + 1;
    if (high != ~0u || size < 4096u || size > 0x40000u || (size & (size - 1u)) ||
        (base & (size - 1u)) || base>=(1ull<<52) || base+size>(1ull<<52)) {
        pci_command(hc,c & (uint16_t)~PCI_CMD_BUS_MASTER);
        return 0;
    }
    for(unsigned i=0;i<host_count;i++){
        const xhci_t *other=&hosts[i];
        if(base<other->mmio_base+other->mmio_size && other->mmio_base<base+size)
            return 0; /* Do not reset an already claimed controller through an aliased BAR. */
    }
    if (base >= 0x100000000ull) {
        uint32_t bias = (uint32_t)base & 0x1fffffu;
        if(bias+size>0x200000u)return 0;
        int window=paging_mmio64_init(base-bias);
        if(window<0)return 0;
        hc->cap=(volatile uint8_t *)(PAGING_MMIO64_VA+(unsigned)window+bias);
        hc->high_mmio = 1;
        dmesg_write("xHCI: using PAE window for BAR above 4 GiB");
    } else {
        if (base + size > 0x100000000ull) return 0;
        for (uint32_t i = 0; i < size; i += 4096u)
            if (paging_map((uint32_t)base + i, (uint32_t)base + i, PAGING_MMIO_FLAGS) != 0) return 0;
        hc->cap = (volatile uint8_t *)(uint32_t)base;
    }
    pci_command(hc,(c & (uint16_t)~PCI_CMD_BUS_MASTER) | PCI_CMD_MEM_SPACE | INT_DISABLE);
    hc->mmio_base=base;
    hc->mmio_size=size;
    return (int)size;
}

static int setup(xhci_t *hc,const pci_device_t *selected) {
    hc->pdev=selected;
    int size = map_pci(hc,hc->pdev);
    if (!size) {
        dmesg_write("xHCI: unavailable MMIO BAR");
        return 0;
    }
    uint32_t cap0 = rd(hc,hc->cap, 0), hcs1 = rd(hc,hc->cap, 4), hcs2 = rd(hc,hc->cap, 8), hcc = rd(hc,hc->cap, 0x10);
    uint32_t cap_len = cap0 & 0xffu, max_ports = hcs1 >> 24;
    uint32_t door = rd(hc,hc->cap, 0x14) & ~3u, run = rd(hc,hc->cap, 0x18) & ~31u;
    hc->slot_count = hcs1 & 0xffu;
    hc->port_count = max_ports > ROOT_PORTS ? ROOT_PORTS : max_ports;
    hc->stride = (hcc & 4u) ? 16u : 8u; /* DWORDs per context */
    if ((cap0 >> 16) < 0x100u || cap_len < 0x20u || !hc->slot_count || !hc->port_count ||
        cap_len + PORTSC(max_ports) > (uint32_t)size || door < cap_len || run < cap_len ||
        door > (uint32_t)size - (hc->slot_count + 1u) * 4u || run > (uint32_t)size - 0x40u ||
        !prepare_protocols(hc,(hcc >> 16) * 4u, (uint32_t)size)) {
        dmesg_write("xHCI: firmware handoff or capability check failed");
        return 0;
    }
    hc->op = hc->cap + cap_len; hc->rt = hc->cap + run; hc->db = hc->cap + door;
    if (!wait_reg(hc,hc->op, 4, 1u << 11, 0, 1000)) goto fail;
    wr(hc,hc->op, 0, 0);
    if (!wait_reg(hc,hc->op, 4, 1, 1, 1000)) goto fail;
    wr(hc,hc->op, 0, 2); /* Host Controller Reset */
    if (!wait_reg(hc,hc->op, 0, 2, 0, 1000) || !wait_reg(hc,hc->op, 4, 1u << 11, 0, 1000) ||
        !(rd(hc,hc->op, 8) & 1u)) goto fail;

    uint32_t n = ((hcs2 >> 27) & 31u) | ((hcs2 >> 16) & 0x3e0u);
    if (n > SCRATCH_MAX) goto fail;
    if (n) {
        uint32_t phys = pmm_alloc_pages(n);
        if (!phys) goto fail;
        memset((void *)phys, 0, n * 4096u);
        for (uint32_t i = 0; i < n; i++) hc->scratch[i] = phys + i * 4096u;
        hc->dcbaa[0] = addr(hc->scratch);
    }
    ring_init(&hc->command);
    hc->event_cycle = 1;
    hc->erst.lo = addr(hc->events);
    hc->erst.status = EVENT_N;
    /* The controller may fetch ERST as soon as ERSTBA is written, even
     * before Run/Stop is set. Enable DMA after reset but before ERSTBA. */
    uint16_t pc = pci_read_word(hc->pdev->bus, hc->pdev->dev, hc->pdev->func, PCI_REG_COMMAND);
    pci_command(hc,pc | PCI_CMD_BUS_MASTER);
    fence();
    wr(hc,hc->rt, 0x20, 1u);        /* Interrupter 0 disabled; clear pending flag */
    wr(hc,hc->rt, 0x28, 1u);        /* ERSTSZ */
    wr64(hc,hc->rt, 0x38, addr(hc->events));
    wr64(hc,hc->rt, 0x30, addr(&hc->erst));
    wr64(hc,hc->op, 0x30, addr(hc->dcbaa));
    wr64(hc,hc->op, 0x18, addr(hc->command.trb) | 1u);
    wr(hc,hc->op, 0x38, hc->slot_count);
    wr(hc,hc->op, 0, 1);
    if (!wait_reg(hc,hc->op, 4, 1, 0, 1000)) goto fail;
    hc->running = 1;

    for (uint32_t i = 0; i < hc->port_count; i++) {
        uint32_t p = rd(hc,hc->op, PORTSC(i));
        if (!(p & (1u << 9))) wr(hc,hc->op, PORTSC(i), (p & PORT_RW) | (1u << 9));
    }
    for (uint32_t i = 0; i < hc->port_count && hc->connected < (int)DEVICES; i++)
        if (rd(hc,hc->op, PORTSC(i)) & 1u) hc->ports[hc->connected++] = (uint8_t)i;
    dmesg_write("xHCI: controller ready (root-port HID polling)");
    return 1;
fail:
    stop_controller(hc,"xHCI: initialization failed; controller disabled");
    return 0;
}

void xhci_init(void) {
    for(int i=0;i<pci_device_count() && host_count<XHCI_MAX_CTL;i++){
        const pci_device_t *p=pci_get_device(i);
        if(!p || p->class_code!=0x0cu || p->subclass!=3 || p->prog_if!=0x30)continue;
        xhci_t *hc=&hosts[host_count];
        memset(hc,0,sizeof(*hc));
        if(setup(hc,p))host_count++;
    }
}

int xhci_port_count(unsigned ctl) {
    xhci_t *hc=host(ctl);
    return hc ? hc->connected : 0;
}
int xhci_port_get(unsigned ctl,int i) {
    xhci_t *hc=host(ctl);
    return !hc || i<0 || i>=hc->connected ? -1 : hc->ports[i];
}

static int address_port(xhci_t *hc,unsigned root,unsigned route,unsigned speed,unsigned tt,unsigned depth) {
    uint32_t i = 0;
    while (i < DEVICES && hc->devices[i].slot) i++;
    if (i == DEVICES) return -1;
    int slot = cmd(hc,TRB(9)|((uint32_t)hc->slot_type[root]<<16),0); /* Enable Slot */
    if (slot <= 0 || (uint32_t)slot > hc->slot_count) return -1;
    device_t *d = &hc->devices[i];
    memset(d, 0, sizeof(*d));
    d->slot = (uint8_t)slot;
    d->speed = (uint8_t)speed;
    d->root=(uint8_t)root;
    d->depth=(uint8_t)depth;
    d->route=route;
    d->tt=tt;
    d->mps0 = speed <= 2 ? 8 : speed == 3 ? 64 : 0; /* 0 encodes 512 here */
    ring_init(&d->ep0);
    hc->dcbaa[slot] = addr(d->out);
    ic(hc,d, 0)[1] = 3u;
    ic(hc,d, 1)[0] = route | (speed << 20) | (1u << 27);
    ic(hc,d, 1)[1] = (root + 1u) << 16;
    ic(hc,d, 1)[2] = tt;
    ic(hc,d, 2)[1] = ((d->mps0 ? (uint32_t)d->mps0 : 512u) << 16) | (4u << 3) | (3u << 1);
    ic(hc,d, 2)[2] = addr(d->ep0.trb) | 1u;
    ic(hc,d, 2)[4] = 8;
    if (cmd(hc,TRB(11) | ((uint32_t)slot << 24), addr(d->in)) < 0) { /* Address Device */
        if (cmd(hc,TRB(10) | ((uint32_t)slot << 24), 0) >= 0) {
            hc->dcbaa[slot] = 0; d->slot = 0;
        }
        dmesg_write("xHCI: root-port Address Device failed");
        return -1;
    }
    unsigned handle=route ? ROOT_PORTS+i : root;
    hc->port_device[handle]=(uint8_t)(i+1);
    return (int)handle;
}

int xhci_port_start(unsigned ctl,int port) {
    xhci_t *hc=host(ctl);
    if(!hc){return 0;}
    if (device_at(hc,port)) return 1;
    if (!hc->running || port < 0 || (uint32_t)port >= hc->port_count) return 0;
    uint32_t offset = PORTSC(port), ps = rd(hc,hc->op, offset);
    if (!(ps & 1u)) return 0;
    /* USB2 root ports require PR. An enabled SuperSpeed port is already
     * initialized by the link state machine, and needs no USB2 reset. */
    if (((ps >> 10) & 15u) <= 3 || !(ps & 2u)) {
        wr(hc,hc->op, offset, (ps & PORT_RW) | (1u << 4));
        if (!wait_reg(hc,hc->op, offset, (1u << 4) | 3u, 3u, 250)) return 0;
    }
    ps = rd(hc,hc->op, offset);
    wr(hc,hc->op, offset, (ps & PORT_RW) | (ps & PORT_CHANGES));
    uint32_t speed = (ps >> 10) & 15u;
    if (!speed || speed > 4) return 0;
    return address_port(hc,(unsigned)port,0,speed,0,0)>=0;
}

unsigned xhci_hub_depth(unsigned ctl,unsigned port) {
    xhci_t *hc=host(ctl);
    if(!hc){return XHCI_HUB_TIERS;}
    device_t *d=device_at(hc,(int)port);
    return d ? d->depth : XHCI_HUB_TIERS;
}

unsigned xhci_device_speed(unsigned ctl,unsigned port){
    xhci_t *hc=host(ctl);
    if(!hc){return 0;}
    device_t *d=device_at(hc,(int)port);
    return d ? d->speed : 0;
}

int xhci_hub_configure(unsigned ctl,unsigned port,unsigned ports,unsigned think_time) {
    xhci_t *hc=host(ctl);
    if(!hc){return -1;}
    device_t *d=device_at(hc,(int)port);
    if(!d || !ports || ports>15 || d->speed==2 || d->depth>=XHCI_HUB_TIERS)return -1;
    memset(d->in,0,sizeof(d->in));
    ic(hc,d,0)[1]=1;
    uint32_t *slot=ic(hc,d,1);
    memcpy(slot,oc(hc,d,0),hc->stride*sizeof(uint32_t));
    slot[0]|=1u<<26; /* hub context */
    slot[1]=(slot[1]&0x00ffffffu)|(ports<<24);
    if(d->speed==3)slot[2]=(slot[2]&~(3u<<16))|((think_time&3u)<<16);
    slot[3]=0;
    if(cmd(hc,TRB(12)|((uint32_t)d->slot<<24),addr(d->in))<0)return -1;
    d->is_hub=1;
    return 0;
}

int xhci_hub_child(unsigned ctl,unsigned hub,unsigned port,unsigned speed) {
    xhci_t *hc=host(ctl);
    if(!hc){return -1;}
    device_t *parent=device_at(hc,(int)hub);
    if(!parent || !parent->is_hub || parent->depth>=XHCI_HUB_TIERS ||
       !port || port>15 || !speed || speed>4 || ((parent->speed==4)!=(speed==4)))return -1;
    /* Inherit the upstream high-speed Hub's translator through FS hubs. */
    unsigned tt=speed>=3 ? 0 : parent->speed==3 ? parent->slot|(port<<8) : parent->tt;
    unsigned route=parent->route|(port<<(parent->depth*4u));
    return address_port(hc,parent->root,route,speed,tt,parent->depth+1u);
}

unsigned xhci_root_ports(unsigned ctl) {
    xhci_t *hc=host(ctl);
    /* Keep topology visible after a fault so its inputs can be retired. */
    return hc ? hc->port_count : 0;
}

unsigned xhci_root_status(unsigned ctl,unsigned port) {
    xhci_t *hc=host(ctl);
    if(!hc){return 0;}
    if (!hc->running || port >= hc->port_count) return 0;
    uint32_t p=rd(hc,hc->op,PORTSC(port));
    if (p&PORT_CHANGES) wr(hc,hc->op,PORTSC(port),(p&PORT_RW)|(p&PORT_CHANGES));
    return (p&1u)|((p>>16)&2u);
}

void xhci_port_stop(unsigned ctl,unsigned port) {
    xhci_t *hc=host(ctl);
    if(!hc){return;}
    uint32_t flags=irq_save_disable();
    device_t *d=device_at(hc,(int)port);
    if (d) hc->port_device[port]=0;
    irq_restore(flags);
    if (!d) return;
    /* Keep DMA backing memory intact until Disable Slot has completed. */
    unsigned slot=d->slot;
    if (cmd(hc,TRB(10)|(slot<<24),0)>=0) {
        hc->dcbaa[slot]=0;
        d->slot=0;
    }
}

static int evaluate_mps(xhci_t *hc,device_t *d, uint8_t mps) {
    memset(d->in, 0, sizeof(d->in));
    ic(hc,d, 0)[1] = 2u;
    memcpy(ic(hc,d, 2), oc(hc,d, 1), hc->stride * sizeof(uint32_t));
    ic(hc,d, 2)[1] = (ic(hc,d, 2)[1] & 0xffffu) | ((uint32_t)mps << 16);
    if (cmd(hc,TRB(13) | ((uint32_t)d->slot << 24), addr(d->in)) < 0) return -1;
    d->mps0 = mps;
    return 0;
}

int xhci_control(unsigned ctl,int port, const uint8_t s[8], int in, uint8_t *buf, int len, int *actual) {
    xhci_t *hc=host(ctl);
    if(!hc){if(actual)*actual=0; return -1;}
    device_t *d = device_at(hc,port);
    if (actual) *actual = 0;
    if (!d || d->ctrl_dead || !s || len < 0 || len > (int)sizeof(d->ctrl_buf) ||
        (len && !buf) || len != (s[6] | (s[7] << 8))) return -1;
    if (s[0] == 0 && s[1] == 5 && len == 0) return 0; /* hardware already assigned it */
    if (len && !in) memcpy(d->ctrl_buf, buf, (uint32_t)len);
    uint32_t lo = 0, hi = 0;
    for (uint32_t i = 0; i < 4; i++) {
        lo |= (uint32_t)s[i] << (8u * i);
        hi |= (uint32_t)s[i + 4] << (8u * i);
    }
    d->ctrl_code = d->ctrl_residual = d->ctrl_data = 0;
    enqueue(&d->ep0, lo, hi, 8, TRB(2) | IDT | ((len ? (in ? 3u : 2u) : 0u) << 16));
    if (len)
        d->ctrl_data = enqueue(&d->ep0, addr(d->ctrl_buf), 0, (uint32_t)len,
                               TRB(3) | (in ? ((1u << 16) | ISP) : 0));
    d->ctrl_status = enqueue(&d->ep0, 0, 0, 0, TRB(4) | IOC | (len && in ? 0 : 1u << 16));
    fence();
    wr(hc,hc->db, d->slot * 4u, 1);
    int ok = wait_event(hc,&d->ctrl_code,0);
    d->ctrl_status = 0;
    if (!ok) {d->ctrl_dead=1;return -1;}
    if (d->ctrl_code != 1 && d->ctrl_code != 13) {
        uint32_t bits = ((uint32_t)d->slot << 24) | (1u << 16);
        if (d->ctrl_code == 6 || d->ctrl_code == 4 || d->ctrl_code == 3) {
            if (cmd(hc,TRB(14) | bits, 0) >= 0)
                (void)cmd(hc,TRB(16) | bits, addr(&d->ep0.trb[d->ep0.next]) | d->ep0.cycle);
        }
        return -1;
    }
    if (d->ctrl_residual > (uint32_t)len) return -1;
    len -= (int)d->ctrl_residual;
    if (in && len) memcpy(buf, d->ctrl_buf, (uint32_t)len);
    if (in && len >= 8 && s[0] == 0x80 && s[1] == 6 && s[3] == 1 && d->speed == 1) {
        uint8_t mps = buf[7];
        if (mps != 8 && mps != 16 && mps != 32 && mps != 64) return -1;
        if (mps != d->mps0 && evaluate_mps(hc,d, mps) != 0) return -1;
    }
    if (actual) *actual = len;
    return 0;
}

int xhci_configure_hid(unsigned ctl,int port, uint8_t ep, uint16_t mps, uint8_t interval) {
    xhci_t *hc=host(ctl);
    if(!hc){return -1;}
    device_t *d = device_at(hc,port);
    uint32_t dci = 2u * (ep & 15u) + 1u;
    if(!d || ep<0x81 || ep>0x8f || !interval || !mps || mps>64)return -1;
    hid_ep_t *h=0;
    unsigned max=dci;
    for(unsigned j=0;j<XHCI_HID_ENDPOINTS;j++){
        if(d->hid[j].dci==dci)return -1;
        if(!d->hid[j].dci && !h)h=&d->hid[j];
        if(d->hid[j].dci>max)max=d->hid[j].dci;
    }
    if(!h)return -1;
    uint32_t iv = interval - 1u;
    if (d->speed <= 2) {
        uint32_t uf = (uint32_t)interval * 8u;
        iv = 0;
        while ((uf >>= 1) != 0) iv++;
    }
    if (iv > 15) return -1;
    ring_init(&h->ring);
    memset(d->in, 0, sizeof(d->in));
    ic(hc,d, 0)[1] = 1u | (1u << dci);
    memcpy(ic(hc,d, 1), oc(hc,d, 0), hc->stride * sizeof(uint32_t));
    ic(hc,d, 1)[0] = (ic(hc,d, 1)[0] & 0x07ffffffu) | (max << 27);
    ic(hc,d, 1)[3] = 0;
    uint32_t *e = ic(hc,d, dci + 1);
    e[0] = iv << 16;
    e[1] = ((uint32_t)mps << 16) | (7u << 3) | (3u << 1);
    e[2] = addr(h->ring.trb) | 1u;
    e[4] = ((uint32_t)mps << 16) | mps;
    if (cmd(hc,TRB(12) | ((uint32_t)d->slot << 24), addr(d->in)) < 0) return -1;
    h->dci = (uint8_t)dci;
    h->mps = (uint8_t)mps;
    return 0;
}

int xhci_interrupt_in(unsigned ctl,int port, uint8_t ep, uint8_t *buf, int len, int *actual) {
    xhci_t *hc=host(ctl);
    if(!hc){if(actual)*actual=0; return -1;}
    if (actual) *actual = 0;
    if (!buf || len < 1) return -1;
    uint32_t flags = irq_save_disable();
    device_t *d = device_at(hc,port);
    if(!d || ep<0x81 || ep>0x8f || !(rd(hc,hc->op,PORTSC(d->root))&1u)) {
        irq_restore(flags);return -1;
    }
    hid_ep_t *h=0;
    const unsigned dci=2u*(ep&15u)+1u;
    for(unsigned j=0;j<XHCI_HID_ENDPOINTS;j++)
        if(d->hid[j].dci==dci){h=&d->hid[j];break;}
    if(!h || h->failed){irq_restore(flags);return -1;}
    poll_events(hc);
    if(h->done) {
        h->done=h->pending=0;
        if((h->code!=1 && h->code!=13) || h->residual>h->mps) {
            h->failed=1; /* only this interface stops; the other one still works */
            irq_restore(flags);return -1;
        }
        int n=h->mps-(int)h->residual;
        if(n>len)n=len;
        memcpy(buf,h->buf,(uint32_t)n);
        if(actual)*actual=n;
    }
    if(!h->pending) {
        h->address=enqueue(&h->ring,addr(h->buf),0,h->mps,TRB(1)|IOC|ISP);
        h->pending=1;
        fence();
        wr(hc,hc->db,d->slot*4u,h->dci);
    }
    irq_restore(flags);
    return 0;
}
