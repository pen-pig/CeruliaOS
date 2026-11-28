#ifndef CERULIA_OHCI_H
#define CERULIA_OHCI_H
#include "types.h"

/* PCI OHCI 1.x, USB1 low/full speed, EP0 and periodic HID IN. */
#define OHCI_MAX_CTL 4u
#define OHCI_MAX_ROOT 15u
void ohci_init(void);
unsigned ohci_root_count(unsigned ctl);
/* Normalized: connected=1, changed=2, low speed=4. */
unsigned ohci_root_status(unsigned ctl,unsigned port,int acknowledge);
void ohci_root_disable(unsigned ctl,unsigned port);
int ohci_reset(unsigned ctl,unsigned port,int downstream);
int ohci_control(unsigned ctl,uint8_t addr,int low,const uint8_t setup[8],
                 int in,uint8_t *buf,int len,int *actual);
int ohci_configure_hid(unsigned ctl,uint8_t addr,uint8_t ep,uint16_t mps,
                       uint8_t interval,int low);
int ohci_interrupt_in(unsigned ctl,unsigned port,uint8_t addr,uint8_t ep,
                       uint8_t *buf,int len,int *actual);
void ohci_release_hid(unsigned ctl,uint8_t addr);
#endif
