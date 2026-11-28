#ifndef XHCI_H
#define XHCI_H

#include "types.h"

/* Independent controller instances. Polled HID/EP0 and hub hotplug. */
#define XHCI_MAX_CTL 4u
#define XHCI_MAX_ROOT 32u
void xhci_init(void);
int xhci_port_count(unsigned ctl);
int xhci_port_get(unsigned ctl,int index);
int xhci_port_start(unsigned ctl,int port);
unsigned xhci_root_ports(unsigned ctl);
/* Returns bit 0 connected, bit 1 connection changed since last sample. */
unsigned xhci_root_status(unsigned ctl,unsigned port);
void xhci_port_stop(unsigned ctl,unsigned port);
#define XHCI_HUB_TIERS 5u
unsigned xhci_hub_depth(unsigned ctl,unsigned port);
unsigned xhci_device_speed(unsigned ctl,unsigned port);
int xhci_hub_configure(unsigned ctl,unsigned port,unsigned ports,unsigned think_time);
int xhci_hub_child(unsigned ctl,unsigned root,unsigned downstream,unsigned speed);
int xhci_control(unsigned ctl,int port, const uint8_t setup[8], int in, uint8_t *buf,
                 int len, int *actual);
int xhci_configure_hid(unsigned ctl,int port, uint8_t ep, uint16_t mps, uint8_t interval);
int xhci_interrupt_in(unsigned ctl,int port, uint8_t ep, uint8_t *buf, int len, int *actual);

#endif
