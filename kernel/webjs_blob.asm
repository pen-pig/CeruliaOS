BITS 32
section .rodata
global webjs_elf_start, webjs_elf_end
webjs_elf_start:
incbin "user/webjs.elf"
webjs_elf_end:
