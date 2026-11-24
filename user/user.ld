/*
 * user/user.ld - 用户程序链接脚本（步骤 5c）
 *
 * 装载地址必须与 kernel/syscall.h 的 USER_IMAGE_BASE 一致（0x00400000），
 * 否则内核 elf_validate 会以 "entry point outside user image area" 拒绝。
 *
 * 给 ELF 头留出页内空间，但不要把头映射成单独的 PT_LOAD，否则 Linux ld
 * 的 separate-code 模式会让两个段重叠在第一页，和来宾逐段映射不兼容。
 * text/rodata 只读，data/bss 可写，物理页边界对齐。
 */
ENTRY(_start)

PHDRS
{
    code PT_LOAD FLAGS(5);  /* PF_R | PF_X */
    data PT_LOAD FLAGS(6);  /* PF_R | PF_W */
}

SECTIONS
{
    . = 0x00400000 + SIZEOF_HEADERS;

    .text   : { *(.text*) } :code
    /* Keep data in the same read-only segment without letting CPU guards
     * mistake Unicode or math tables for executable machine code. */
    .rodata : { *(.rodata*) } :code
    .data   : ALIGN(0x1000) { *(.data*) } :data
    /* If .data is absent, .bss must still start on a new page. */
    .bss    : ALIGN(0x1000) { *(.bss*) *(COMMON) } :data

    /DISCARD/ : { *(.comment) *(.eh_frame*) *(.note*) }
}
