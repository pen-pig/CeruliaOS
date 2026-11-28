#include <assert.h>
#include <stdio.h>
#include "../kernel/ehci_periodic.h"

int main(void) {
    ehci_period_t p[8];
    assert(ehci_eps(0,0)==0x2000 && ehci_eps(3,0)==0 && ehci_eps(3,1)==0x1000);
    assert(ehci_tt_bits(3,7)==0x03830000 && !ehci_tt_bits(0,7));
    assert(!ehci_period_plan(0,1,p,0,&p[0]));
    assert(!ehci_period_plan(15,0,p,0,&p[0]));
    assert(ehci_period_plan(2,0,p,0,&p[0])==0x55 && p[0].frames==1);
    assert(ehci_period_plan(6,0,p,1,&p[1])==1 && p[1].frames==4);
    assert(ehci_period_plan(10,42,p,2,&p[2])==0x1c01 && p[2].frames==8);
    assert(ehci_period_plan(8,42,p,3,&p[3])==0x1c01 && p[3].phase==1);
    assert(ehci_period_plan(1,42,p,4,&p[4])==0xe008 && p[4].slot==1);
    assert(!ehci_period_plan(1,42,p,5,&p[5])); /* same TT, both slots occupied */
    assert(ehci_period_plan(1,43,p,5,&p[5])==0x1c01); /* different TT */
    assert(ehci_period_plan(11,42,p,6,&p[6])==0x1c01 && p[6].phase==2);
    assert(ehci_period_plan(255,43,p,7,&p[7])==0xe008 && p[7].frames==128);
    unsigned order[]={7,2,3,6,1,0,4,5};
    for(unsigned f=0;f<1024;f++){
        unsigned i=0, seen=0;
        while(i<8 && !ehci_period_matches(p[order[i]],f))i++;
        while(i<8){
            assert(ehci_period_matches(p[order[i]],f));
            seen|=1u<<order[i];
            unsigned j=i+1;
            while(j<8 && !ehci_period_overlap(p[order[i]],p[order[j]]))j++;
            i=j;
        }
        for(unsigned j=0;j<8;j++)
            assert(((seen>>j)&1u)==ehci_period_matches(p[j],f));
    }
    puts("PASS EHCI TT route bits, masks, budget and all 1024 frame-chain entries");
}
