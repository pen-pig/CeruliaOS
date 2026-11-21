; The firmware's PE Authenticode verification covers this read-only section.
; Secure builds execute these bytes, not the mutable FAT32 KERNEL.BIN.
section .rdata
global embedded_kernel, embedded_kernel_end
embedded_kernel:
    incbin KERNEL_FILE
embedded_kernel_end:
