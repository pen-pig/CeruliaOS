/* xHCI 1.x polling driver for direct-connected boot HID devices. DMA uses
 * kernel-owned rings and bounded bounce buffers, never caller addresses.
 * No hub, hotplug, streams, isochronous, or bulk transport in this driver. */
#include "xhci.h"
#include "pci.h"
#include "paging.h"
#include "pmm.h"
#include "isr.h"
#include "irqflags.h"
#include "mem.h"
#include "dmesg.h"

#define ROOT_PORTS 32u
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
typedef struct {
    uint32_t out[32 * 16] __attribute__((aligned(4096)));
    uint32_t in[33 * 16] __attribute__((aligned(4096)));
    ring_t ep0, hid;
    uint8_t ctrl_buf[256] __attribute__((aligned(256)));
    uint8_t hid_buf[64] __attribute__((aligned(64)));
    uint8_t slot, speed, mps0, hid_dci, hid_mps, hid_pending, hid_done, hid_code;
    uint32_t ctrl_status, ctrl_data, ctrl_code, ctrl_residual;
    uint32_t hid_addr, hid_residual;
} device_t;

static device_t devices[DEVICES];
static uint64_t dcbaa[256] __attribute__((aligned(64)));
static uint64_t scratch[SCRATCH_MAX] __attribute__((aligned(64)));
static trb_t events[EVENT_N] __attribute__((aligned(4096)));
static trb_t erst __attribute__((aligned(64)));
static ring_t command;
static const pci_device_t *pdev;
static volatile uint8_t *cap, *op, *rt, *db;
static uint32_t event_next, event_cycle, stride, cmd_addr, cmd_code, cmd_slot;
static uint8_t ports[ROOT_PORTS], port_device[ROOT_PORTS];
static uint32_t slot_count, port_count;
static int connected, running, high_mmio;

static uint32_t addr(const void *p) { return (uint32_t)p; }
static uint32_t rd(volatile uint8_t *p, uint32_t off) {
    if (high_mmio) return paging_mmio64_read((uint32_t)p - PAGING_MMIO64_VA + off);
    return *(volatile uint32_t *)(p + off);
}
static void wr(volatile uint8_t *p, uint32_t off, uint32_t v) {
    if (high_mmio) {
        paging_mmio64_write((uint32_t)p - PAGING_MMIO64_VA + off, v);
        return;
    }
    *(volatile uint32_t *)(p + off) = v;
}
static void wr64(volatile uint8_t *p, uint32_t off, uint32_t lo) {
    wr(p, off, lo);
    wr(p, off + 4, 0);
}
static void pci_command(uint16_t cmd) {
    /* Writing zero to PCI status leaves its W1C bits alone. */
    pci_write_dword(pdev->bus, pdev->dev, pdev->func, PCI_REG_COMMAND, cmd);
}
static void fence(void) { asm volatile("" ::: "memory"); }

static int wait_reg(volatile uint8_t *p, uint32_t off, uint32_t mask,
                    uint32_t wanted, uint32_t timeout) {
    uint32_t start = g_pit_ticks;
    for (uint32_t i = 0; i < 20000000u; i++) {
        if ((rd(p, off) & mask) == wanted) return 1;
        if ((uint32_t)(g_pit_ticks - start) >= timeout) break;
        asm volatile("pause");
    }
    return 0;
}

static void stop_controller(const char *why) {
    running = 0;
    connected = 0;
    if (op) wr(op, 0, rd(op, 0) & ~1u);
    uint16_t c = pci_read_word(pdev->bus, pdev->dev, pdev->func, PCI_REG_COMMAND);
    pci_command((c & (uint16_t)~PCI_CMD_BUS_MASTER) | INT_DISABLE);
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

static device_t *device_at(int port) {
    if (!running || port < 0 || (uint32_t)port >= port_count || !port_device[port])
        return 0;
    return &devices[port_device[port] - 1];
}

static void poll_events(void) {
    /* Work is bounded even if a device keeps generating port events. */
    uint32_t seen = 0;
    for (; seen < EVENT_N; seen++) {
        volatile trb_t *e = &events[event_next];
        uint32_t ctl = e->ctl;
        if ((ctl & CYCLE) != event_cycle) break;
        fence();
        uint32_t type = (ctl >> 10) & 0x3fu;
        uint32_t slot = ctl >> 24, dci = (ctl >> 16) & 31u;
        uint32_t ptr = e->lo, status = e->status, code = status >> 24;
        if (type == 33 && !e->hi && ptr == cmd_addr) {
            cmd_slot = slot;
            cmd_code = code;
        } else if (type == 32 && !e->hi) {
            for (uint32_t i = 0; i < DEVICES; i++) {
                device_t *d = &devices[i];
                if (!d->slot || d->slot != slot) continue;
                if (dci == 1 && d->ctrl_status) {
                    if (ptr == d->ctrl_data && code == 13) {
                        d->ctrl_residual = status & 0xffffffu;
                    } else if (ptr == d->ctrl_status || (code != 1 && code != 13)) {
                        d->ctrl_code = code;
                    }
                } else if (dci == d->hid_dci && d->hid_pending && ptr == d->hid_addr) {
                    d->hid_residual = status & 0xffffffu;
                    d->hid_code = (uint8_t)code;
                    d->hid_done = 1;
                }
                break;
            }
        }
        if (++event_next == EVENT_N) { event_next = 0; event_cycle ^= 1u; }
    }
    if (seen) wr64(rt, 0x38, addr(&events[event_next]) | 8u); /* ERDP/EHB */
}

static int wait_event(const uint32_t *code) {
    uint32_t start = g_pit_ticks;
    for (uint32_t i = 0; i < 20000000u; i++) {
        poll_events();
        if (*code) return 1;
        if ((uint32_t)(g_pit_ticks - start) >= 500u || (rd(op, 4) & 0x1001u)) break;
        asm volatile("pause");
    }
    stop_controller("xHCI: event timeout; controller disabled");
    return 0;
}

static int cmd(uint32_t type_bits, uint32_t lo) {
    if (!running) return -1;
    cmd_code = 0;
    cmd_addr = enqueue(&command, lo, 0, 0, type_bits);
    fence();
    wr(db, 0, 0);
    if (!wait_event(&cmd_code) || cmd_code != 1) return -1;
    return (int)cmd_slot;
}

static uint32_t *ic(device_t *d, uint32_t index) { return d->in + index * stride; }
static uint32_t *oc(device_t *d, uint32_t index) { return d->out + index * stride; }

static int bios_handoff(uint32_t off, uint32_t bar_size) {
    for (int i = 0; off && i < 64; i++) {
        if (off > bar_size - 4u) return 0;
        uint32_t cap_reg = rd(cap, off);
        if ((cap_reg & 0xffu) == 1) {
            wr(cap, off, cap_reg | (1u << 24)); /* OS-owned semaphore */
            return wait_reg(cap, off, 1u << 16, 0, 1000);
        }
        uint32_t next = ((cap_reg >> 8) & 0xffu) * 4u;
        if (!next) return 1;
        off += next;
    }
    return off == 0;
}

static int map_pci(const pci_device_t *p) {
    uint32_t bar = p->bar[0], is64 = (bar & 6u) == 4u;
    uint64_t base = (bar & ~15u) | (is64 ? (uint64_t)p->bar[1] << 32 : 0);
    if ((bar & 1u) || (bar & 6u) == 2u || base < PAGING_IDENTITY_END) return 0;
    uint16_t c = pci_read_word(p->bus, p->dev, p->func, PCI_REG_COMMAND);
    pci_command((c & (uint16_t)~(PCI_CMD_MEM_SPACE | PCI_CMD_BUS_MASTER)) | INT_DISABLE);
    pci_write_dword(p->bus, p->dev, p->func, PCI_REG_BAR0, 0xffffffffu);
    if (is64) pci_write_dword(p->bus, p->dev, p->func, PCI_REG_BAR0 + 4, 0xffffffffu);
    uint32_t mask = pci_read_dword(p->bus, p->dev, p->func, PCI_REG_BAR0);
    uint32_t high = is64 ? pci_read_dword(p->bus, p->dev, p->func, PCI_REG_BAR0 + 4) : ~0u;
    pci_write_dword(p->bus, p->dev, p->func, PCI_REG_BAR0, bar);
    if (is64) pci_write_dword(p->bus, p->dev, p->func, PCI_REG_BAR0 + 4, p->bar[1]);
    uint32_t size = ~(mask & ~15u) + 1;
    if (high != ~0u || size < 4096u || size > 0x40000u || (size & (size - 1u)) ||
        (base & (size - 1u))) {
        pci_command(c & (uint16_t)~PCI_CMD_BUS_MASTER);
        return 0;
    }
    if (base >= 0x100000000ull) {
        uint32_t bias = (uint32_t)base & 0x1fffffu;
        if (bias + size > 0x200000u || paging_mmio64_init(base - bias) != 0) return 0;
        cap = (volatile uint8_t *)(PAGING_MMIO64_VA + bias);
        high_mmio = 1;
        dmesg_write("xHCI: using PAE window for BAR above 4 GiB");
    } else {
        if (base + size > 0x100000000ull) return 0;
        for (uint32_t i = 0; i < size; i += 4096u)
            if (paging_map((uint32_t)base + i, (uint32_t)base + i, PAGING_MMIO_FLAGS) != 0) return 0;
        cap = (volatile uint8_t *)(uint32_t)base;
    }
    pci_command((c & (uint16_t)~PCI_CMD_BUS_MASTER) | PCI_CMD_MEM_SPACE | INT_DISABLE);
    return (int)size;
}

void xhci_init(void) {
    for (int i = 0; i < pci_device_count(); i++) {
        const pci_device_t *p = pci_get_device(i);
        if (p && p->class_code == 0x0c && p->subclass == 3 && p->prog_if == 0x30) {
            pdev = p;
            break;
        }
    }
    if (!pdev) return;
    int size = map_pci(pdev);
    if (!size) {
        dmesg_write("xHCI: unavailable MMIO BAR");
        return;
    }
    uint32_t cap0 = rd(cap, 0), hcs1 = rd(cap, 4), hcs2 = rd(cap, 8), hcc = rd(cap, 0x10);
    uint32_t cap_len = cap0 & 0xffu, max_ports = hcs1 >> 24;
    uint32_t door = rd(cap, 0x14) & ~3u, run = rd(cap, 0x18) & ~31u;
    slot_count = hcs1 & 0xffu;
    port_count = max_ports > ROOT_PORTS ? ROOT_PORTS : max_ports;
    stride = (hcc & 4u) ? 16u : 8u; /* DWORDs per context */
    if ((cap0 >> 16) < 0x100u || cap_len < 0x20u || !slot_count || !port_count ||
        cap_len + PORTSC(max_ports) > (uint32_t)size || door < cap_len || run < cap_len ||
        door > (uint32_t)size - (slot_count + 1u) * 4u || run > (uint32_t)size - 0x40u ||
        !bios_handoff((hcc >> 16) * 4u, (uint32_t)size)) {
        dmesg_write("xHCI: firmware handoff or capability check failed");
        return;
    }
    op = cap + cap_len; rt = cap + run; db = cap + door;
    if (!wait_reg(op, 4, 1u << 11, 0, 1000)) goto fail;
    wr(op, 0, 0);
    if (!wait_reg(op, 4, 1, 1, 1000)) goto fail;
    wr(op, 0, 2); /* Host Controller Reset */
    if (!wait_reg(op, 0, 2, 0, 1000) || !wait_reg(op, 4, 1u << 11, 0, 1000) ||
        !(rd(op, 8) & 1u)) goto fail;

    uint32_t n = ((hcs2 >> 27) & 31u) | ((hcs2 >> 16) & 0x3e0u);
    if (n > SCRATCH_MAX) goto fail;
    if (n) {
        uint32_t phys = pmm_alloc_pages(n);
        if (!phys) goto fail;
        memset((void *)phys, 0, n * 4096u);
        for (uint32_t i = 0; i < n; i++) scratch[i] = phys + i * 4096u;
        dcbaa[0] = addr(scratch);
    }
    ring_init(&command);
    event_cycle = 1;
    erst.lo = addr(events);
    erst.status = EVENT_N;
    /* The controller may fetch ERST as soon as ERSTBA is written, even
     * before Run/Stop is set. Enable DMA after reset but before ERSTBA. */
    uint16_t pc = pci_read_word(pdev->bus, pdev->dev, pdev->func, PCI_REG_COMMAND);
    pci_command(pc | PCI_CMD_BUS_MASTER);
    fence();
    wr(rt, 0x20, 1u);        /* Interrupter 0 disabled; clear pending flag */
    wr(rt, 0x28, 1u);        /* ERSTSZ */
    wr64(rt, 0x38, addr(events));
    wr64(rt, 0x30, addr(&erst));
    wr64(op, 0x30, addr(dcbaa));
    wr64(op, 0x18, addr(command.trb) | 1u);
    wr(op, 0x38, slot_count);
    wr(op, 0, 1);
    if (!wait_reg(op, 4, 1, 0, 1000)) goto fail;
    running = 1;

    for (uint32_t i = 0; i < port_count; i++) {
        uint32_t p = rd(op, PORTSC(i));
        if (!(p & (1u << 9))) wr(op, PORTSC(i), (p & PORT_RW) | (1u << 9));
    }
    for (uint32_t i = 0; i < port_count && connected < (int)DEVICES; i++)
        if (rd(op, PORTSC(i)) & 1u) ports[connected++] = (uint8_t)i;
    dmesg_write("xHCI: controller ready (root-port HID polling)");
    return;
fail:
    stop_controller("xHCI: initialization failed; controller disabled");
}

int xhci_port_count(void) { return connected; }
int xhci_port_get(int i) { return i < 0 || i >= connected ? -1 : ports[i]; }

int xhci_port_start(int port) {
    if (!running || port < 0 || (uint32_t)port >= port_count) return 0;
    if (device_at(port)) return 1;
    uint32_t offset = PORTSC(port), ps = rd(op, offset);
    if (!(ps & 1u)) return 0;
    /* USB2 root ports require PR. An enabled SuperSpeed port is already
     * initialized by the link state machine, and needs no USB2 reset. */
    if (((ps >> 10) & 15u) <= 3 || !(ps & 2u)) {
        wr(op, offset, (ps & PORT_RW) | (1u << 4));
        if (!wait_reg(op, offset, (1u << 4) | 3u, 3u, 250)) return 0;
    }
    ps = rd(op, offset);
    wr(op, offset, (ps & PORT_RW) | (ps & PORT_CHANGES));
    uint32_t speed = (ps >> 10) & 15u;
    if (!speed || speed > 4) return 0;
    uint32_t i = 0;
    while (i < DEVICES && devices[i].slot) i++;
    if (i == DEVICES) return 0;
    int slot = cmd(TRB(9), 0); /* Enable Slot */
    if (slot <= 0 || (uint32_t)slot > slot_count) return 0;
    device_t *d = &devices[i];
    memset(d, 0, sizeof(*d));
    d->slot = (uint8_t)slot;
    d->speed = (uint8_t)speed;
    d->mps0 = speed <= 2 ? 8 : speed == 3 ? 64 : 0; /* 0 encodes 512 here */
    ring_init(&d->ep0);
    dcbaa[slot] = addr(d->out);
    ic(d, 0)[1] = 3u;
    ic(d, 1)[0] = (speed << 20) | (1u << 27);
    ic(d, 1)[1] = ((uint32_t)port + 1u) << 16;
    ic(d, 2)[1] = ((d->mps0 ? (uint32_t)d->mps0 : 512u) << 16) | (4u << 3) | (3u << 1);
    ic(d, 2)[2] = addr(d->ep0.trb) | 1u;
    ic(d, 2)[4] = 8;
    if (cmd(TRB(11) | ((uint32_t)slot << 24), addr(d->in)) < 0) { /* Address Device */
        if (cmd(TRB(10) | ((uint32_t)slot << 24), 0) >= 0) {
            dcbaa[slot] = 0; d->slot = 0;
        }
        dmesg_write("xHCI: root-port Address Device failed");
        return 0;
    }
    port_device[port] = (uint8_t)(i + 1);
    return 1;
}

static int evaluate_mps(device_t *d, uint8_t mps) {
    memset(d->in, 0, sizeof(d->in));
    ic(d, 0)[1] = 2u;
    memcpy(ic(d, 2), oc(d, 1), stride * sizeof(uint32_t));
    ic(d, 2)[1] = (ic(d, 2)[1] & 0xffffu) | ((uint32_t)mps << 16);
    if (cmd(TRB(13) | ((uint32_t)d->slot << 24), addr(d->in)) < 0) return -1;
    d->mps0 = mps;
    return 0;
}

int xhci_control(int port, const uint8_t s[8], int in, uint8_t *buf, int len, int *actual) {
    device_t *d = device_at(port);
    if (actual) *actual = 0;
    if (!d || !s || len < 0 || len > (int)sizeof(d->ctrl_buf) ||
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
    wr(db, d->slot * 4u, 1);
    int ok = wait_event(&d->ctrl_code);
    d->ctrl_status = 0;
    if (!ok) return -1;
    if (d->ctrl_code != 1 && d->ctrl_code != 13) {
        uint32_t bits = ((uint32_t)d->slot << 24) | (1u << 16);
        if (d->ctrl_code == 6 || d->ctrl_code == 4 || d->ctrl_code == 3) {
            if (cmd(TRB(14) | bits, 0) >= 0)
                (void)cmd(TRB(16) | bits, addr(&d->ep0.trb[d->ep0.next]) | d->ep0.cycle);
        }
        return -1;
    }
    if (d->ctrl_residual > (uint32_t)len) return -1;
    len -= (int)d->ctrl_residual;
    if (in && len) memcpy(buf, d->ctrl_buf, (uint32_t)len);
    if (in && len >= 8 && s[0] == 0x80 && s[1] == 6 && s[3] == 1 && d->speed == 1) {
        uint8_t mps = buf[7];
        if (mps != 8 && mps != 16 && mps != 32 && mps != 64) return -1;
        if (mps != d->mps0 && evaluate_mps(d, mps) != 0) return -1;
    }
    if (actual) *actual = len;
    return 0;
}

int xhci_configure_hid(int port, uint8_t ep, uint16_t mps, uint8_t interval) {
    device_t *d = device_at(port);
    uint32_t dci = 2u * (ep & 15u) + 1u;
    if (!d || !(ep & 0x80u) || dci < 3 || !interval || !mps || mps > sizeof(d->hid_buf))
        return -1;
    uint32_t iv = interval - 1u;
    if (d->speed <= 2) {
        uint32_t uf = (uint32_t)interval * 8u;
        iv = 0;
        while ((uf >>= 1) != 0) iv++;
    }
    if (iv > 15) return -1;
    ring_init(&d->hid);
    memset(d->in, 0, sizeof(d->in));
    ic(d, 0)[1] = 1u | (1u << dci);
    memcpy(ic(d, 1), oc(d, 0), stride * sizeof(uint32_t));
    ic(d, 1)[0] = (ic(d, 1)[0] & 0x07ffffffu) | (dci << 27);
    ic(d, 1)[3] = 0;
    uint32_t *e = ic(d, dci + 1);
    e[0] = iv << 16;
    e[1] = ((uint32_t)mps << 16) | (7u << 3) | (3u << 1);
    e[2] = addr(d->hid.trb) | 1u;
    e[4] = ((uint32_t)mps << 16) | mps;
    if (cmd(TRB(12) | ((uint32_t)d->slot << 24), addr(d->in)) < 0) return -1;
    d->hid_dci = (uint8_t)dci;
    d->hid_mps = (uint8_t)mps;
    return 0;
}

int xhci_interrupt_in(int port, uint8_t ep, uint8_t *buf, int len, int *actual) {
    if (actual) *actual = 0;
    if (!buf || len < 1) return -1;
    uint32_t flags = irq_save_disable();
    device_t *d = device_at(port);
    if (!d || !d->hid_dci || d->hid_dci != 2u * (ep & 15u) + 1u ||
        !(rd(op, PORTSC(port)) & 1u)) { irq_restore(flags); return -1; }
    poll_events();
    if (d->hid_done) {
        d->hid_done = d->hid_pending = 0;
        if ((d->hid_code != 1 && d->hid_code != 13) || d->hid_residual > d->hid_mps) {
            d->hid_dci = 0;   /* terminal fault: leave endpoint quiescent */
            irq_restore(flags);
            return -1;
        }
        int n = d->hid_mps - (int)d->hid_residual;
        if (n > len) n = len;
        memcpy(buf, d->hid_buf, (uint32_t)n);
        if (actual) *actual = n;
    }
    if (!d->hid_pending) {
        d->hid_addr = enqueue(&d->hid, addr(d->hid_buf), 0, d->hid_mps, TRB(1) | IOC | ISP);
        d->hid_pending = 1;
        fence();
        wr(db, d->slot * 4u, d->hid_dci);
    }
    irq_restore(flags);
    return 0;
}
