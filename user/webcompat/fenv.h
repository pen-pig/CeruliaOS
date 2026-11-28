#pragma once
#define FE_TONEAREST 0
#define FE_DOWNWARD 0x400
#define FE_UPWARD 0x800
#define FE_TOWARDZERO 0xc00
static inline int fegetround(void){unsigned short w;__asm__ volatile("fnstcw %0":"=m"(w));return w&0xc00;}
static inline int fesetround(int r){
    unsigned short w;__asm__ volatile("fnstcw %0":"=m"(w));w=(w&~0xc00)|(r&0xc00);
    __asm__ volatile("fldcw %0"::"m"(w));return 0;
}
