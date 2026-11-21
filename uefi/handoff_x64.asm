; Relocatable x64 EFI -> i686 kernel trampoline. The loader copies this code
; to executable pages below 4 GiB; there are no image-relative relocations.
; Microsoft x64 ABI: RCX=kernel source, EDX=size, R8=UEFI handoff page.
bits 64
default rel
section .text
global efi_x64_thunk, efi_x64_thunk_end

efi_x64_thunk:
    cli
    cld
    mov esi, ecx
    mov ebx, edx
    mov ebp, r8d

    ; Leave PCID off before dropping CR0.PG. GDT and both modes of code
    ; remain identity-mapped in firmware-addressable RAM below 4 GiB.
    mov rax, cr4
    btr rax, 17
    mov cr4, rax
    sub rsp, 16
    lea rax, [gdt]
    mov word [rsp], gdt_end - gdt - 1
    mov [rsp + 2], rax
    lgdt [rsp]
    lea rax, [compat32]
    push 0x08
    push rax
    retfq

bits 32
compat32:
    ; CS is now a flat 32-bit segment. The firmware stack may have been
    ; above 4 GiB; do not touch it or call any code until the kernel sets ESP.
    mov eax, cr0
    and eax, 0x7fffffff
    mov cr0, eax
    mov ecx, 0xc0000080          ; IA32_EFER
    rdmsr
    and eax, ~((1 << 8) | (1 << 11))  ; LME, NXE
    wrmsr
    xor eax, eax
    mov cr4, eax

    mov ax, 0x10
    mov ds, ax
    mov es, ax
    mov fs, ax
    mov gs, ax
    mov ss, ax

    ; Only overwrite the fixed kernel destinations after firmware paging
    ; is gone; a firmware page table or stack might have occupied them.
    mov edi, 0x01C00000          ; KERNEL_DST (boot/layout.inc)
    mov ecx, ebx
    rep movsb
    mov esi, ebp
    mov edi, 0x5000             ; single packed UEFI handoff page
    mov ecx, 4096 / 4
    rep movsd
    mov eax, 0x01C00000
    jmp eax

align 8, db 0
gdt:
    dq 0x0000000000000000, 0x00cf9a000000ffff, 0x00cf92000000ffff
gdt_end:
efi_x64_thunk_end:
