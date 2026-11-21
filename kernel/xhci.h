#ifndef XHCI_H
#define XHCI_H

#include "types.h"

/* One controller, boot-time root-port enumeration, direct USB HID IN only. */
void xhci_init(void);
int xhci_port_count(void);
int xhci_port_get(int index);
int xhci_port_start(int port);
int xhci_control(int port, const uint8_t setup[8], int in, uint8_t *buf,
                 int len, int *actual);
int xhci_configure_hid(int port, uint8_t ep, uint16_t mps, uint8_t interval);
int xhci_interrupt_in(int port, uint8_t ep, uint8_t *buf, int len, int *actual);

#endif
