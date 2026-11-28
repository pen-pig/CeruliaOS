/* PCI OpenHCI 1.x. Register/data layout: OpenHCI 1.0a, chapters 4 and 7.
 * Polling driver, using below-4-GiB kernel-owned, cache-coherent DMA.
 * Control TDs span packet boundaries; periodic HID has independent ED/TDs.
 * No mass storage, isochronous endpoints, or support for non-PCI OHCI blocks. */
#include "ohci.h"
#include "pci.h"
#include "paging.h"
#include "irqflags.h"
#include "isr.h"
#include "mem.h"
#include "dmesg.h"

enum {REV=0, CONTROL=4, CMD=8, INTR=12, INTR_OFF=20, HCCA=24,
    CONTROL_HEAD=32, CONTROL_CURRENT=36,
      FM_INTERVAL=52, FM_NUMBER=60, PERIOD_START=64, LS_THRESHOLD=68,
      RH_A=72, RH_STATUS=80, RH_PORT=84};
enum {PLE=4,CLE=16,HCFS=3u<<6,OPERATIONAL=2u<<6,IR=1u<<8,
      HCR=1,CLF=2,OCR=8,WDH=2,UE=16,
      CCS=1,PES=2,PRS=1u<<4,PPS=1u<<8,LSDA=1u<<9,CSC=1u<<16,
      RH_CHANGES=31u<<16};
enum {ED_SKIP=1u<<14,ED_LOW=1u<<13,ED_CARRY=2,ED_HALT=1,
      TD_R=1u<<18,TD_OUT=1u<<19,TD_IN=2u<<19,TD_DATA0=2u<<24,
      TD_DATA1=3u<<24,TD_PENDING=15u<<28};
#define HID_EDS 4u
#define TIERS 6u

typedef struct {volatile uint32_t info,tail,head,next;} ed_t;
typedef struct {volatile uint32_t info,cbp,next,end;} td_t;
typedef struct {
    volatile uint32_t table[32];
    volatile uint16_t frame,pad;
    volatile uint32_t done;
    uint8_t reserved[120];
} hcca_t;
_Static_assert(sizeof(hcca_t)==256,"OHCI HCCA size");
_Static_assert(sizeof(ed_t)==16 && sizeof(td_t)==16,"OHCI descriptor size");
typedef struct {
    ed_t ed __attribute__((aligned(16)));
    td_t td[2];
    uint8_t data[64] __attribute__((aligned(64)));
    uint8_t addr,ep,mps,tier,pending,failed,released;
} hid_t;
typedef struct {
    hcca_t hcca __attribute__((aligned(256)));
    ed_t tier[TIERS] __attribute__((aligned(16)));
    ed_t control_ed __attribute__((aligned(16)));
    td_t control_td[4] __attribute__((aligned(16)));
    uint8_t data[256] __attribute__((aligned(256)));
    uint8_t setup[8] __attribute__((aligned(16)));
    hid_t hid[HID_EDS];
    uint8_t ep0[128];
    volatile uint32_t *mmio;
    const pci_device_t *pci;
    unsigned ports,running,control_busy,control_done;
} hc_t;
static hc_t controllers[OHCI_MAX_CTL];

static uint32_t dma(const void *p) {return (uint32_t)p;}
static void fence(void) {asm volatile("":::"memory");}
static uint32_t rd(const hc_t *c,unsigned r){return c->mmio[r/4];}
static void wr(hc_t *c,unsigned r,uint32_t v){c->mmio[r/4]=v;}
static void delay(unsigned ms) {
    uint32_t t=g_pit_ticks;
    while((uint32_t)(g_pit_ticks-t)<ms)asm volatile("pause");
}
static int wait_reg(hc_t *c,unsigned r,uint32_t mask,uint32_t value,unsigned ms){
    uint32_t t=g_pit_ticks;
    for(unsigned i=0;i<20000000u;i++){
        if((rd(c,r)&mask)==value)return 1;
        if((uint32_t)(g_pit_ticks-t)>=ms)return 0;
        asm volatile("pause");
    }
    return 0;
}
static hc_t *live(unsigned ctl){
    return ctl<OHCI_MAX_CTL && controllers[ctl].running ? &controllers[ctl] : 0;
}
static void stop(hc_t *c){
    uint32_t f=irq_save_disable();
    c->running=0;
    wr(c,INTR_OFF,0xffffffffu);
    wr(c,CONTROL,0);
    const pci_device_t *p=c->pci;
    uint16_t pc=pci_read_word(p->bus,p->dev,p->func,PCI_REG_COMMAND);
    pci_write_dword(p->bus,p->dev,p->func,PCI_REG_COMMAND,pc&~PCI_CMD_BUS_MASTER);
    irq_restore(f);
    dmesg_write("OHCI: DMA disabled after controller fault");
}

/* Reclaim a TD only after the HC publishes it on the HCCA done queue.
 * Never follow an unknown DMA pointer, and always bound the walk. */
static void done(hc_t *c){
    if(!(rd(c,INTR)&WDH))return;
    uint32_t ptr=c->hcca.done&~15u;
    fence();
    for(unsigned n=0;ptr && n<4u+HID_EDS;n++){
        td_t *td=0;
        for(unsigned i=0;i<4;i++){
            if(ptr==dma(&c->control_td[i])) {
                td=&c->control_td[i];
                c->control_done|=1u<<i;
                break;
            }
        }
        for(unsigned i=0;!td && i<HID_EDS;i++){
            hid_t *h=&c->hid[i];
            if(ptr==dma(h->td)){
                td=h->td;
                h->released=1;
            }
        }
        if(!td)break;
        ptr=td->next&~15u;
    }
    c->hcca.done=0;
    fence();
    wr(c,INTR,WDH);
}

static void publish_periodic(hc_t *c) {
    uint32_t f=irq_save_disable();
    for(unsigned k=0;k<TIERS;k++) {
        uint32_t next=k ? dma(&c->tier[k-1]) : 0;
        c->tier[k].info=ED_SKIP;
        for(unsigned i=0;i<HID_EDS;i++) {
            hid_t *h=&c->hid[i];
            if(h->addr && h->tier==k){h->ed.next=next;next=dma(&h->ed);}
        }
        c->tier[k].next=next;
    }
    for(unsigned i=0;i<32;i++){
        unsigned tier=0;
        while(tier<TIERS-1 && !(i&(1u<<tier)))tier++;
        c->hcca.table[i]=dma(&c->tier[tier]);
    }
    fence();
    irq_restore(f);
}

static int wait_frames(hc_t *c){
    uint32_t start=g_pit_ticks;
    uint16_t frame=(uint16_t)rd(c,FM_NUMBER);
    while((uint16_t)((uint16_t)rd(c,FM_NUMBER)-frame)<2u){
        if((uint32_t)(g_pit_ticks-start)>=25u){stop(c);return 0;}
        asm volatile("pause");
    }
    uint32_t flags=irq_save_disable();
    done(c);
    irq_restore(flags);
    return 1;
}

static int pause_periodic(hc_t *c){
    wr(c,CONTROL,rd(c,CONTROL)&~PLE);
    /* Let host-frame reads finish before ED links or DMA storage change. */
    return wait_frames(c);
}

static int setup_controller(hc_t *c,const pci_device_t *p) {
    uint32_t bar=p->bar[0],base=bar&~15u;
    if((bar&7u) || base<PAGING_IDENTITY_END || (base&4095u))return 0;
    if(paging_map(base,base,PAGING_MMIO_FLAGS)!=0)return 0;
    c->mmio=(volatile uint32_t *)base;c->pci=p;
    uint16_t pc=pci_read_word(p->bus,p->dev,p->func,PCI_REG_COMMAND);
    if(!(pc&PCI_CMD_MEM_SPACE))
        pci_write_dword(p->bus,p->dev,p->func,PCI_REG_COMMAND,pc|PCI_CMD_MEM_SPACE);
    unsigned rev=rd(c,REV)&255u;
    if(rev!=0x10u && rev!=0x11u)return 0;
    if(rd(c,CONTROL)&IR){
        wr(c,CMD,OCR);
        if(!wait_reg(c,CONTROL,IR,0,1000)){
            dmesg_write("OHCI: firmware retained controller");
            return 0;
        }
    }
    unsigned interval=rd(c,FM_INTERVAL)&0x3fffu;
    if(interval<10000u || interval>13000u)interval=11999u;
    wr(c,INTR_OFF,0xffffffffu);
    wr(c,CONTROL,0);
    /* Leave firmware's DMA/IRQ settings alone until it grants ownership. */
    pc=(pc|PCI_CMD_MEM_SPACE|(1u<<10))&~PCI_CMD_BUS_MASTER;
    pci_write_dword(p->bus,p->dev,p->func,PCI_REG_COMMAND,pc);
    delay(10);
    wr(c,CMD,HCR);
    if(!wait_reg(c,CMD,HCR,0,50))return 0;
    unsigned ports=rd(c,RH_A)&255u;
    if(!ports || ports>OHCI_MAX_ROOT)return 0;
    c->ports=ports;
    publish_periodic(c);
    wr(c,HCCA,dma(&c->hcca));
    wr(c,CONTROL_HEAD,dma(&c->control_ed));
    wr(c,CONTROL_CURRENT,0);
    uint32_t fit=(rd(c,FM_INTERVAL)^0x80000000u)&0x80000000u;
    wr(c,FM_INTERVAL,fit|(((interval-210u)*6u/7u)<<16)|interval);
    wr(c,PERIOD_START,interval*9u/10u);
    wr(c,LS_THRESHOLD,0x628u);
    wr(c,INTR,0xffffffffu);
    pci_write_dword(p->bus,p->dev,p->func,PCI_REG_COMMAND,pc|PCI_CMD_BUS_MASTER);
    wr(c,CONTROL,OPERATIONAL|PLE|CLE);
    c->running=1;
    uint32_t rh=rd(c,RH_A);
    if(!(rh&(1u<<9))){
        if(rh&(1u<<8))
            for(unsigned i=0;i<c->ports;i++)wr(c,RH_PORT+4*i,PPS);
        else wr(c,RH_STATUS,1u<<16);
    }
    delay(((rh>>24)&255u)*2u+25u);
    if(rd(c,INTR)&UE){stop(c);return 0;}
    dmesg_write("OHCI: controller ready (USB HID)");
    return 1;
}

void ohci_init(void){
    unsigned used=0;
    for(int i=0;i<pci_device_count() && used<OHCI_MAX_CTL;i++) {
        const pci_device_t *p=pci_get_device(i);
        if(!p || p->class_code!=0x0c || p->subclass!=3 || p->prog_if!=0x10)continue;
        if(setup_controller(&controllers[used],p))used++;
    }
}

unsigned ohci_root_count(unsigned ctl){
    /* Keep the root topology after a fatal error so the input worker can
     * retire its devices (root_status returns disconnected in that case). */
    return ctl<OHCI_MAX_CTL ? controllers[ctl].ports : 0;
}
unsigned ohci_root_status(unsigned ctl,unsigned port,int acknowledge) {
    hc_t *c=live(ctl);if(!c || port>=c->ports)return 0;
    uint32_t ps=rd(c,RH_PORT+4*port);
    if(acknowledge && (ps&RH_CHANGES))wr(c,RH_PORT+4*port,ps&RH_CHANGES);
    return (ps&CCS)|((ps&CSC)?2u:0u)|((ps&LSDA)?4u:0u);
}
void ohci_root_disable(unsigned ctl,unsigned port){
    hc_t *c=live(ctl);
    if(c && port<c->ports)wr(c,RH_PORT+4*port,CCS); /* ClearPortEnable */
}
int ohci_reset(unsigned ctl,unsigned port,int downstream){
    hc_t *c=live(ctl);
    if(!c || port>=c->ports)return 0;
    c->ep0[0]=0;
    if(downstream)return 1;
    unsigned reg=RH_PORT+4*port;
    if(!(rd(c,reg)&CCS))return 0;
    wr(c,reg,RH_CHANGES);
    wr(c,reg,PRS);
    if(!wait_reg(c,reg,PRS|CCS|PES,CCS|PES,250))return 0;
    wr(c,reg,rd(c,reg)&RH_CHANGES);
    delay(12);
    return (rd(c,reg)&(CCS|PES))==(CCS|PES);
}

static void make_td(td_t *td,unsigned flags,const uint8_t *buf,unsigned len,td_t *next){
    td->info=flags|TD_PENDING;
    td->cbp=len ? dma(buf) : 0;
    td->end=len ? dma(buf)+len-1 : 0;
    td->next=dma(next);
}
static int bytes_done(const td_t *td,const uint8_t *buffer,int len){
    uint32_t ptr=td->cbp, begin=dma(buffer);
    if(!ptr)return len;
    if(ptr<begin || ptr-begin>(unsigned)len)return -1;
    return (int)(ptr-begin);
}

int ohci_control(unsigned ctl,uint8_t addr,int low,const uint8_t setup[8],
                 int in,uint8_t *buf,int len,int *actual) {
    hc_t *c=live(ctl);
    if(actual)*actual=0;
    if(!c || addr>127 || !setup || len<0 || len>(int)sizeof(c->data) ||
       (len && !buf) || len!=(setup[6]|(setup[7]<<8)))return -1;
    uint32_t flags=irq_save_disable();
    if(!flags || c->control_busy){irq_restore(flags);return -1;}
    c->control_busy=1;
    irq_restore(flags);
    unsigned mps=low ? 8u : (c->ep0[addr]?c->ep0[addr]:8u);
    memcpy(c->setup,setup,8);
    if(len && !in)memcpy(c->data,buf,(unsigned)len);
    td_t *td=c->control_td;
    const unsigned status=len?2u:1u,tail=status+1u;
    ed_t *ed=&c->control_ed;
    c->control_done=0;
    make_td(td,TD_DATA0,c->setup,8,td+1);
    if(len)make_td(td+1,TD_DATA1|(in?TD_IN|TD_R:TD_OUT),
                   c->data,(unsigned)len,td+2);
    make_td(td+status,TD_DATA1|(in?TD_OUT:TD_IN),0,0,td+tail);
    ed->tail=dma(td+tail);
    ed->head=dma(td);
    fence();
    ed->info=addr|(low?ED_LOW:0)|(mps<<16);
    wr(c,CMD,CLF);
    int n=-1;
    uint32_t begin=g_pit_ticks;
    while((uint32_t)(g_pit_ticks-begin)<500u) {
        flags=irq_save_disable();
        done(c);
        irq_restore(flags);
        if(rd(c,INTR)&UE){stop(c);break;}
        if(ed->head&ED_HALT)break;
        if((c->control_done&(1u<<status)) && (ed->head&~15u)==ed->tail &&
           (td[status].info>>28)==0){
            n=len?bytes_done(td+1,c->data,len):0;
            break;
        }
        asm volatile("pause");
    }
    ed->info|=ED_SKIP;
    if(n<0 && c->running){
        wr(c,CONTROL,rd(c,CONTROL)&~CLE);
        if(wait_frames(c)){
            wr(c,CONTROL_CURRENT,0);
            ed->head=ed->tail;
            wr(c,CONTROL,rd(c,CONTROL)|CLE);
        }
    }
    if(n>=0){
        if(in && n)memcpy(buf,c->data,(unsigned)n);
        if(in && n>=8 && setup[0]==0x80 && setup[1]==6 && setup[3]==1){
            uint8_t size=buf[7];
            if(size==8 || size==16 || size==32 || size==64)c->ep0[addr]=size;
        }
        if(setup[0]==0 && setup[1]==5 && setup[2]<128)
            c->ep0[setup[2]]=c->ep0[0];
        if(actual)*actual=n;
    }
    flags=irq_save_disable();
    c->control_busy=0;
    irq_restore(flags);
    return n<0?-1:0;
}

int ohci_configure_hid(unsigned ctl,uint8_t addr,uint8_t ep,uint16_t mps,
                       uint8_t interval,int low) {
    hc_t *c=live(ctl);
    if(!c || !addr || addr>127 || ep<0x81 || ep>0x8f || !interval ||
       !mps || mps>(low?8:64))return -1;
    hid_t *h=0;
    for(unsigned i=0;i<HID_EDS;i++){
        if(c->hid[i].addr==addr && c->hid[i].ep==ep)return -1;
        if(!c->hid[i].addr && !h)h=&c->hid[i];
    }
    if(!h || !pause_periodic(c))return -1;
    unsigned tier=0;
    while(tier<TIERS-1 && (2u<<tier)<=interval)tier++;
    h->ed.tail=h->ed.head=dma(&h->td[1]);
    h->ed.info=addr|((uint32_t)(ep&15u)<<7)|(low?ED_LOW:0)|((uint32_t)mps<<16);
    h->addr=addr;h->ep=ep;h->mps=(uint8_t)mps;h->tier=(uint8_t)tier;
    h->pending=h->failed=h->released=0;
    publish_periodic(c);
    wr(c,CONTROL,rd(c,CONTROL)|PLE);
    return 0;
}

int ohci_interrupt_in(unsigned ctl,unsigned port,uint8_t addr,uint8_t ep,
                       uint8_t *buf,int len,int *actual) {
    hc_t *c=live(ctl);
    if(actual)*actual=0;
    if(!c || port>=c->ports || !buf || len<1 || !(rd(c,RH_PORT+4*port)&CCS))return -1;
    uint32_t flags=irq_save_disable();
    hid_t *h=0;
    for(unsigned i=0;i<HID_EDS;i++)
        if(c->hid[i].addr==addr && c->hid[i].ep==ep){h=&c->hid[i];break;}
    if(!h || h->failed){irq_restore(flags);return -1;}
    if(rd(c,INTR)&UE){stop(c);irq_restore(flags);return -1;}
    done(c);
    if(h->pending) {
        uint32_t head=h->ed.head;
        if(head&ED_HALT){h->failed=1;irq_restore(flags);return -1;}
        if(!h->released || (head&~15u)!=h->ed.tail){irq_restore(flags);return 0;}
        int n=bytes_done(&h->td[0],h->data,h->mps);
        if(n<0 || (h->td[0].info>>28)!=0){h->failed=1;irq_restore(flags);return -1;}
        if(n>len)n=len;
        if(n)memcpy(buf,h->data,(unsigned)n);
        if(actual)*actual=n;
    }
    h->released=0;
    make_td(h->td,TD_IN|TD_R,h->data,h->mps,&h->td[1]);
    fence();
    h->ed.head=dma(h->td)|(h->ed.head&ED_CARRY);
    h->pending=1;
    irq_restore(flags);
    return 0;
}

void ohci_release_hid(unsigned ctl,uint8_t addr){
    hc_t *c=live(ctl);
    if(!c || !pause_periodic(c))return;
    for(unsigned i=0;i<HID_EDS;i++)if(c->hid[i].addr==addr){
        c->hid[i].failed=1;
        c->hid[i].addr=0;
    }
    publish_periodic(c);
    wr(c,CONTROL,rd(c,CONTROL)|PLE);
}
