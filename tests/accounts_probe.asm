; Ring 3 fixture: the syscall boundary must enforce the same policy as shell.
bits 32
org 0x00400000

db 0x7f, "ELF", 1, 1, 1
times 9 db 0
dw 2, 3
dd 1, entry, ph - $$, 0, 0
dw ph - $$, entry - ph, 1, 0, 0, 0
ph:
dd 1, 0, $$, $$, finish - $$, finish - $$, 5, 4096

%macro open_file 2
    mov eax, 5
    mov ebx, %1
    mov ecx, %2
    int 0x80
%endmacro
%macro print 2
    mov eax, 1
    mov ebx, 1
    mov ecx, %1
    mov edx, %2
    int 0x80
%endmacro

entry:
    open_file forbidden, 0
    test eax, eax
    jns fail
    open_file escape, 0x241       ; O_WRONLY | O_CREAT | O_TRUNC
    test eax, eax
    jns fail
    open_file database, 0
    test eax, eax
    jns fail
    open_file allowed, 0
    test eax, eax
    js fail
    mov ebx, eax
    mov eax, 6
    int 0x80
    print passed, passed_len
    xor ebx, ebx
    jmp exit
fail:
    print failed, failed_len
    mov ebx, 1
exit:
    mov eax, 2
    int 0x80

forbidden: db "/home/root/secret.txt", 0
escape: db "/home/alice/../bob/made.txt", 0
database: db "/etc/accounts.db", 0
allowed: db "myfile.txt", 0
passed: db "account syscalls isolated", 10
passed_len equ $ - passed
failed: db "probe failure", 10
failed_len equ $ - failed
finish:
