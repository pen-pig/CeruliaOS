/* Host test for the descriptor classifier used by the kernel. */
#include <stdint.h>
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../kernel/usbwheel.h"
#include "../kernel/usbkeylayout.h"
int main(void) {
    const uint8_t header[]={5,1,9,2,0xa1,1,9,1,0xa1,0,5,9,0x19,1,0x29,3,
        0x15,0,0x25,1,0x75,1,0x95,3,0x81,2,0x75,5,0x95,1,0x81,1,
        5,1,0x15,0x81,0x25,127};
    const uint8_t axes[]={0x75,8,0x95,2,9,0x30,9,0x31,0x81,6,0x95,1,9,0x38,0x81,6,0xc0,0xc0};
    uint8_t desc[128];unsigned off=sizeof header, n=off+sizeof axes;
    memcpy(desc,header,off);memcpy(desc+off,axes,sizeof axes);
    assert(hid_wheel_layout(desc,n));
    for(unsigned i=0;i<n;i++)assert(!hid_wheel_layout(desc,i));
    desc[off+9]=2; assert(!hid_wheel_layout(desc,n)); /* absolute X/Y */
    desc[off+9]=6;desc[off+13]=0x35; assert(!hid_wheel_layout(desc,n)); /* not a wheel */
    desc[off+13]=0x38;desc[off]=0x85; assert(!hid_wheel_layout(desc,n)); /* report ID */
    desc[off]=0x75;desc[off+3]=255; assert(!hid_wheel_layout(desc,n));
    const uint8_t standard[]={
        5,1,9,6,0xa1,1,5,7,0x19,224,0x29,231,0x15,0,0x25,1,
        0x75,1,0x95,8,0x81,2,0x75,8,0x95,1,0x81,1,
        5,7,0x19,0,0x29,101,0x15,0,0x25,101,0x75,8,0x95,6,0x81,0,0xc0
    };
    assert(hid_key_layout(standard,sizeof standard)==HID_KEY_NO_ID);
    memcpy(desc,standard,6);
    desc[6]=0x85;desc[7]=7;
    memcpy(desc+8,standard+6,sizeof standard-6);
    unsigned numbered=sizeof standard+2;
    assert(hid_key_layout(desc,numbered)==7);
    for(unsigned i=0;i<numbered;i++)assert(!hid_key_layout(desc,i));
    desc[7]=0;assert(!hid_key_layout(desc,numbered));
    desc[7]=7;desc[numbered-4]=0xff;assert(!hid_key_layout(desc,numbered));
    puts("PASS keyboard report ID, boot-shaped report mode and invalid/truncated descriptors");
    puts("PASS split-axis wheel descriptor, all truncated prefixes, absolute axes, report ID and invalid counts");
    return 0;
}
