; Small guest-only fixture for descriptor lifetime and process fault tests.
bits 32
org 0x00400000

db 0x7f, "ELF", 1, 1, 1
times 9 db 0
dw 2, 3
dd 1, entry, ph - $$, 0, 0
dw ph - $$, entry - ph, 1, 0, 0, 0
ph:
dd 1, 0, $$, $$, end - $$, end - $$, 5, 4096

%macro print 2
    mov eax, 1
    mov ebx, 1
    mov ecx, %1
    mov edx, %2
    int 0x80
%endmacro

entry:
%if CASE = 4
    std                         ; exception entry must clear DF before calling C
    mov eax, [0x50000000]       ; unmapped address, user #PF
%else
    mov eax, 5
    mov ebx, target
    mov ecx, 0x201              ; O_WRONLY | O_TRUNC (must already exist)
    int 0x80
    test eax, eax
    js fail
    mov ebp, eax
    mov ebx, eax
    mov eax, 1
    mov ecx, payload
    mov edx, payload_len
    int 0x80
    cmp eax, payload_len
    jne fail
%if CASE = 3
    print staged, staged_len
    xor eax, eax
    mov ds, ax                  ; exception entry cannot assume valid user DS
    ud2                         ; user #UD, with an uncommitted dirty fd
%else
    print staged, staged_len
.wait:
    mov ecx, 65536
.delay:
    pause
    loop .delay
    mov eax, 5
    mov ebx, gate
    xor ecx, ecx
    int 0x80
    test eax, eax
    js .wait
    mov ebx, eax
    mov eax, 6
    int 0x80
    mov ebx, ebp
    mov eax, 6
    int 0x80
%if CASE = 1
    test eax, eax
    jnz fail
    print committed, committed_len
%else
    test eax, eax
    jns fail
    print rejected, rejected_len
%endif
    mov eax, 2
    xor ebx, ebx
    int 0x80
%endif
%endif
fail:
    print failed, failed_len
    mov eax, 2
    mov ebx, 1
    int 0x80

%if CASE = 1
target: db "TARGET.TXT", 0
gate: db "/CWD.OK", 0
%else
target: db "/TARGET.TXT", 0
gate: db "/DISK.OK", 0
%endif
payload: db "committed-data"
payload_len equ $ - payload
staged: db "fd buffer staged", 10
staged_len equ $ - staged
committed: db "fd close committed", 10
committed_len equ $ - committed
rejected: db "fd close rejected stale mount", 10
rejected_len equ $ - rejected
failed: db "probe failure", 10
failed_len equ $ - failed
end:
