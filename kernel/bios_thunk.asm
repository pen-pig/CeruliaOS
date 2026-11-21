; ==================================================================
; bios_thunk.asm - 实模式回切调用 INT 10h（VBE 模式设置的唯一通路）
;
; 为什么需要它：boot.asm 只**探测** VBE（0x4F00/0x4F01），不切模式——
; 文本模式要留给内核 shell。切模式原本只有一条路：Bochs VBE_DISPI
; I/O 寄存器（QEMU cirrus 专用）。VMware SVGA / 真机 VESA BIOS 没有
; DISPI，桌面只能落 VGA 0x13 回退（2026-10-20 VMware 实测）。
; 标准 VESA 模式只能用 INT 10h AX=0x4F02 设置，而保护态下没有 VM86，
; 于是做一次轻量实模式回切：
;
;   32 位 PM --(换 trip GDT, 进 16 位段)--> 16 位 PM --(清 PE/PG)-->
;   实模式: int 0x10h --(置 PE)--> 16 位 PM --(远跳)--> 32 位 PM
;
; 关键设计（每一行都是踩过坑的）：
;   - 16 位 stub **运行前 memcpy 到固定线性地址 0x8000**，内部只用
;     0x8xxx 绝对地址与相对跳转，不引用任何链接期符号。选择子 0x800
;     恰好 = 0x8000>>4，实模式远跳后 CS 基址不变，指令流不断。
;   - 进 16 位代码后 ESP 截断成 16 位，内核 32 位栈指针必须先存到
;     .bss（bios_saved_esp），返回后第一件事恢复；DS/ES/SS 也要先换
;     回 32 位数据段（0x10）再碰任何内核内存——16 位数据段限长 64K，
;     拿它访问 19MB 处的内核符号立刻 #GP。
;   - 清 CR0 时 PE 和 PG 必须一起清（PG 在 PE=0 时无定义）；返回时
;     先置 PE 再置 PG，并重载 CR3 冲刷 TLB。
;   - trip GDT 由 C 侧在建（bios_thunk.c），本文件只 lgdt/sgdt。
;   - 返回点地址由 C 侧写进 0x810C——16 位代码编码不了 19MB 的
;     32 位偏移，必须走内存传参。
;
; 低内存布局（回切期间专用，引导期早已用完）：
;   0x8000-0x80FF  16 位 stub 代码（memcpy 目标）
;   0x8100-0x810F  参数/结果槽（见 bios_thunk.c 的常量）
;   0x8180-0x8185  实模式 IVT 伪描述符（limit 0x3FF + base 0，stub 现场填写）
;   0x8200-0x82FF  256 字节 scratch（VBE mode info block 落点）
;   0x8F00         实模式栈顶（向下增长，窗口 0x8000..0x8FFF）
; ==================================================================

[BITS 32]

extern bios_trip_gdt_ptr          ; C 侧：struct { uint16 limit; uint32 base }

global bios_thunk_enter
global bios_stub16_begin
global bios_stub16_end
global bios_back32

section .bss
align 16
bios_saved_gdt:  resb 6           ; sgdt 落点（limit + base）
bios_saved_idt:  resb 6           ; sidt 落点
bios_saved_esp:  resd 1           ; 进入 16 位代码前的 32 位栈指针

section .text

; ------------------------------------------------------------------
; void bios_thunk_enter(void)
; C 侧前置：stub 已复制到 0x8000；参数/回跳地址已写入 0x8100..0x810F；
;           trip GDT 已建好（选择子 0x800 = 16 位代码，基址 0x8000）。
; 返回：INT 10h 的 AX/BX 已由 stub 写回 0x8108/0x810A（C 侧读）。
; ------------------------------------------------------------------
bios_thunk_enter:
    pushfd                        ; 先存 EFLAGS——此刻 IF=1（调用方开着的），
                                  ; 这个值就是返回时要恢复的目标
    cli                           ; 再关中断：回切期间不容打扰
    pushad                        ; 保存全部 GP 寄存器

    sgdt [bios_saved_gdt]
    sidt [bios_saved_idt]
    mov  [bios_saved_esp], esp    ; 指向 pushad 之后的栈顶

    lgdt [bios_trip_gdt_ptr]      ; 换成 trip GDT

    ; 数据段选择子 0x10 在两套 GDT 里语义相同（基址 0 / 限长 4GB），
    ; 无需重载；直接进 16 位代码段（选择子 0x800，基址 0x8000，偏移 0）
    push word 0x0800              ; 66 68：16 位立即数入栈
    push word 0x0000              ; stub 入口偏移（复制镜像的首字节）
    db 0x66
    retf                          ; 66 CB：16 位远返回，弹 IP(16)+CS(16)

; ==================================================================
; 16 位 stub —— 运行前由 C 侧整段 memcpy 到线性 0x8000。
; 只用 0x8xxx 绝对地址与段内相对跳转，不引用链接期符号。
; ==================================================================
[BITS 16]                          ; 关键：stub 必须按 16 位汇编——段描述符
                                   ; D=0，32 位编码的远跳/入栈在实模式下
                                   ; 会错位（2026-10-20 实测：忘切直接跳进
                                   ; IVT 垃圾区挂死）
align 16
bios_stub16_begin:
    ; 入口状态：16 位保护模式，CS=0x800(基址 0x8000)，
    ;           DS/ES/SS=trip 0x10（32 位数据段，基址 0）
    mov  ax, 0x0008               ; trip 16 位数据段（基址 0，限长 64K）
    mov  ds, ax
    mov  es, ax
    mov  ss, ax
    mov  sp, 0x8F00               ; 实模式栈落在 0x8000..0x8FFF 窗口内

    mov  eax, cr0                 ; 66 0F 22 C0（16 位代码里的 32 位操作）
    and  eax, 0x7FFFFFFE          ; 清 PE(bit0) 与 PG(bit31)
    mov  cr0, eax
    ; 实模式远跳：CS 基址仍是 0x8000，冲刷流水线，之后按实模式译码。
    ; IP 必须写 (.rm - bios_stub16_begin) 这个汇编期常量——直接写 .rm
    ; 会生成 R_386_16 绝对重定位，而 .text 链在 19MB，16 位绝对地址
    ; 装不下，ld 直接 "relocation truncated to fit" 拒链接。
    jmp  0x0800:(.rm - bios_stub16_begin)
.rm:
    xor  ax, ax
    mov  ds, ax                   ; 实模式段基 = 选择子<<4，全部归零
    mov  es, ax
    mov  ss, ax                   ; SS 从 0x0008(基 0x80) 拉回 0x0000
    mov  sp, 0x8F00

    mov  ax, [0x8100]             ; AX 参数
    mov  bx, [0x8102]             ; BX 参数
    mov  cx, [0x8104]             ; CX 参数（0x4F01 的模式号）
    mov  di, [0x8106]             ; DI 参数（ES:DI = 0x0000:0x8200）
    ; 实模式 IVT：内核已 lidt 加载自己的 IDT（.bss 高地址），IDTR 还指着
    ; 它——实模式下 int 10h 按 IDTR 取门只会读到内核 IDT 的全零门
    ; （selector 0/flags 0）→ #GP → #DF → 三重故障重启。必须先 lidt 一个
    ; base=0/limit=0x3FF 的伪描述符指回物理 IVT，BIOS 才摸得到。
    ; 返回后由 bios_back32 的 lidt [bios_saved_idt] 恢复内核 IDT。
    mov  word [0x8180], 0x03FF
    mov  dword [0x8182], 0x00000000
    lidt [0x8180]                 ; 16 位 lidt 读 6 字节（limit16 + base32）
    int  0x10                     ; VBE BIOS 调用（中断关着，BIOS 自行开）
    mov  [0x8108], ax             ; AX 结果
    mov  [0x810A], bx             ; BX 结果

    mov  eax, cr0
    or   eax, 0x00000001          ; 重新进保护模式（PG 由 32 侧恢复）
    mov  cr0, eax
    ; 16 位远跳回 trip 32 位代码段 0x18：偏移由 C 侧写在 0x810C
    mov  eax, 0x0018
    push eax                      ; 66 50：16 位模式里的 32 位入栈
    mov  eax, [0x810C]            ; 66 A1：读 C 侧写的返回地址
    push eax
    db 0x66
    retf                          ; 32 位远返回：弹 IP(32)+CS(16)
bios_stub16_end:

; ==================================================================
; 32 位侧返回点（stub 经 0x810C 跳回这里）
; 注意：必须切回 32 位汇编——上面 stub 的 [BITS 16] 会一直作用到
; 下一个 bits 切换指令，漏切会把本段按 16 位编码，popad/ret 等
; 32 位指令全部错位。
; ==================================================================
[BITS 32]
bios_back32:
    mov  eax, 0x0010              ; trip 32 位数据段（基址 0 / 限长 4GB）
    mov  ds, eax                  ; 先修 DS/ES：下面所有内存访问都靠它们，
    mov  es, eax                  ; 16 位数据段限长 64K，摸不到内核符号
    mov  esp, [bios_saved_esp]    ; 恢复 32 位栈指针（此时 SS 仍是 16 位
    mov  ss, eax                  ; 段但不访栈；换 SS 之后才能 popad）

    lgdt [bios_saved_gdt]         ; 恢复内核 GDT
    lidt [bios_saved_idt]         ; 恢复内核 IDT

    ; CS 还停在 trip 的 0x18——内核 GDT 里该选择子不存在，任何中断
    ; （含中断返回时的 CS 重载）都会按内核 GDT 取错描述符，必须先远跳
    ; 回内核代码段选择子 0x08
    mov  eax, 0x0008
    push eax
    mov  eax, .kcs
    push eax
    retf
.kcs:
    mov  eax, cr0
    or   eax, 0x80000000          ; 重新开 PG
    mov  cr0, eax
    mov  eax, cr3
    mov  cr3, eax                 ; 重载 CR3 冲刷 TLB（页表内容未变，保险）

    popad
    popfd                        ; 恢复 IF：把进入 thunk 时的中断状态写回。
                                  ; 2026-10-20 教训：enter 侧曾写成 cli 在
                                  ; pushfd 之前，存下的 EFLAGS 里 IF 已是 0，
                                  ; popfd 原样写回 → 整个桌面带着 IF=0 跑：
                                  ; GUI 循环照画（时钟画面重绘不停）但
                                  ; IRQ0/IRQ1/IRQ12 全部不到，时钟冻结、
                                  ; 键盘鼠标双死，现象极像"循环卡住"。
    ret
