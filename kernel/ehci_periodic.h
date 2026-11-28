#ifndef CERULIA_EHCI_PERIODIC_H
#define CERULIA_EHCI_PERIODIC_H

/* EHCI 1.0, §§3.6 and 4.12. One downstream hub tier. A hub is
 * budgeted as one translator, even if it could expose multiple TTs.
 * At most two small FS/LS IN packets per frame, spaced by 375 us.
 * Start and all three completion opportunities stay in one H-frame. */
typedef struct {
    unsigned short frames, phase;
    unsigned char hub, slot;
} ehci_period_t;

static inline unsigned ehci_eps(unsigned hub,unsigned low) {
    return (hub ? (low ? 1u : 0u) : 2u) << 12;
}
static inline unsigned ehci_tt_bits(unsigned hub,unsigned port) {
    return hub ? (hub << 16) | (port << 23) : 0;
}
static inline unsigned ehci_period_matches(ehci_period_t p,unsigned frame) {
    return (frame & (p.frames-1u))==p.phase;
}
/* For two power-of-two periods the shorter determines whether any
 * instances coincide. Also used to build each QH's successor chain. */
static inline unsigned ehci_period_overlap(ehci_period_t a,ehci_period_t b) {
    unsigned shortest=a.frames<b.frames?a.frames:b.frames;
    return !((a.phase^b.phase)&(shortest-1u));
}
static inline unsigned ehci_period_plan(unsigned interval,unsigned hub,
                                       const ehci_period_t *used,unsigned count,
                                       ehci_period_t *out) {
    if(!interval || interval>255u || hub>127u || (!hub && interval>14u))return 0;
    ehci_period_t p={.frames=1,.hub=(unsigned char)hub};
    if(!hub){
        p.frames=(unsigned short)(interval<=4u?1u:1u<<(interval-4u));
        *out=p;
        return interval==1u?0xffu:interval==2u?0x55u:interval==3u?0x11u:1u;
    }
    while(2u*p.frames<=interval)p.frames*=2u;
    /* Prefer alternating frames over a second slot in the same frame. */
    for(p.slot=0;p.slot<2;p.slot++){
        unsigned start=p.slot*3u;
        for(p.phase=0;p.phase<p.frames;p.phase++){
            unsigned i=0;
            for(;i<count;i++)
                if(used[i].hub==p.hub && used[i].slot==p.slot &&
                   ehci_period_overlap(p,used[i]))break;
            if(i==count){
                *out=p;
                return (1u<<start)|(0x1cu<<(8u+start));
            }
        }
    }
    return 0;
}
#endif
