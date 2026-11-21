ENTRY(_start)
cc_main = 0x401000;
SECTIONS {
    . = 0x400000;
    .text : { *(.text.entry) *(.text*) *(.rodata*) }
    .data : { *(.data*) }
    .bss : { *(.bss*) *(COMMON) }
    ASSERT(SIZEOF(.text) <= 4096, "cc runtime exceeds reserved page")
    ASSERT(SIZEOF(.data) + SIZEOF(.bss) == 0, "cc runtime must be stateless")
    /DISCARD/ : { *(.comment) *(.eh_frame*) *(.note*) }
}
