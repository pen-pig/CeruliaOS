section .rodata align=4
global cjk_bitmap_start, cjk_bitmap_end
cjk_bitmap_start:
    incbin "temp/cjk_bitmap.bin"
cjk_bitmap_end:

align 2, db 0
global gbk_unicode
gbk_unicode:
    incbin "temp/gbk_unicode.bin"

section .note.GNU-stack noalloc noexec nowrite progbits
