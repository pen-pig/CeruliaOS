#include "efi.h"
#include <stddef.h>

/* GOP 协议 GUID: {9042a9de-23dc-4a38-96fb-7aded080516a} */
static const EFI_GUID gEfiGraphicsOutputProtocolGuid = {
  0x9042a9de, 0x23dc, 0x4a38, {0x96,0xfb,0x7a,0xde,0xd0,0x80,0x51,0x6a}
};

/* Kernel image size is dynamic (tools/make_image.py sizes it from kernel_raw.bin).
 * KERNEL_SIZE_MAX only bounds it: the image window is KERNEL_LOAD(19MB)..22MB
 * (3MB); .bss starts at 22MB. The old
 * 0x10000..0xA0000 window (576KB) was a BIOS INT13h limit, not a hardware
 * one: legacy boot now goes through boot/stage2.asm (protected mode) and UEFI
 * never had the limit at all. Must match linker.ld's ASSERT and
 * boot/layout.inc's KERNEL_MAX_BYTES. */
#define KERNEL_SIZE_MAX  0x300000u  /* fail-closed cap; real size read from the file */
#define KERNEL_LOAD  0x01C00000u /* linker.ld / boot/layout.inc: KERNEL_DST */
#define KERNEL_BSS_END 0x02900000u
#define PMM_START 0x02A00000u
#define LOW_KERNEL_END 0x03000000u /* includes PMM through 48 MiB */
#define PAGE_BYTES 4096u

/* ---- 串口（QEMU 里的 0x3F8 就是标准 ISA UART，接 -serial 输出） ---- */
static inline uint8_t inb(uint16_t port) {
  uint8_t v;
  __asm__ __volatile__("inb %1, %0" : "=a"(v) : "Nd"(port));
  return v;
}
static inline void outb(uint16_t port, uint8_t v) {
  __asm__ __volatile__("outb %0, %1" :: "a"(v), "Nd"(port));
}
static void serial_putc(char c) {
  static int serial_off;
  if (serial_off) return;
  for (UINT32 i = 0; i < 4096; ++i) {
    UINT8 status = inb(0x3FD);
    if (status == 0xff) break;
    if (status & 0x20) {
      outb(0x3F8, (uint8_t)c);
      return;
    }
  }
  serial_off = 1;
}
static void serial_puts(const char *s) {
  for (; *s; ++s) serial_putc(*s);
}
static void serial_dec(uint32_t v) {
  char buf[12]; int i = 0;
  if (v == 0) { serial_putc('0'); return; }
  while (v) { buf[i++] = (char)('0' + (v % 10)); v /= 10; }
  while (i > 0) serial_putc(buf[--i]);
}
static void serial_hex(uint32_t v) {
  static const char h[] = "0123456789abcdef";
  serial_putc('0'); serial_putc('x');
  for (int s = 28; s >= 0; s -= 4) serial_putc(h[(v >> s) & 0xF]);
}

/* 屏幕用的 UTF-16 字面量（不用 L""，避免 wchar_t 宽度歧义） */
static const UINT16 msg16[] = {
  'E', 'Z', 'E', 'F', 'I', ':', ' ', 'h', 'e', 'l', 'l', 'o', '\r', '\n', 0
};
#if defined(EZOS_EMBED_KERNEL)
extern const UINT8 embedded_kernel[], embedded_kernel_end[];
#else
static const UINT16 kernel_path[] = {'k','e','r','n','e','l','.','b','i','n',0};
#endif
static const UINT16 secure_boot_name[] = {'S','e','c','u','r','e','B','o','o','t',0};
static const EFI_GUID global_variable_guid = {
  0x8be4df61, 0x93ca, 0x11d2, {0xaa,0x0d,0x00,0xe0,0x98,0x03,0x2b,0x8c}
};

/* Never execute a detached, unsigned kernel if the firmware is enforcing
 * Secure Boot (including if someone enrolls this ordinary loader's hash). */
static int secure_boot_state(EFI_SYSTEM_TABLE *st) {
  EFI_RUNTIME_SERVICES *rt = st->RuntimeServices;
  if (!rt || !rt->GetVariable) return -1;
  UINT8 active = 0;
  UINTN size = sizeof(active);
  EFI_STATUS rc = rt->GetVariable((CHAR16 *)secure_boot_name,
      (EFI_GUID *)&global_variable_guid, 0, &size, &active);
  if (rc == EFI_NOT_FOUND) return 0; /* old UEFI without Secure Boot */
  if (rc != EFI_SUCCESS || size != sizeof(active) || active > 1) return -1;
  return (int)active;
}

/* 内核图形层（kernel/gfx.c）约定的帧缓冲参数区物理布局：
 *   0x5000  uint32  LFB 物理地址（0 = 没有 LFB，回退 VGA 0x13）
 *   0x5004  uint16  XRES / 0x5006  uint16  YRES / 0x5008  uint8  BPP
 * 扩展（供 U3）：0x5009 uint8 PixelFormat / 0x500A uint16 保留 / 0x500C uint32 stride
 * 0x5010  uint32  UEFI 启动魔数 0x55454649（U3 用它判定跳过 VBE 探测）
 * 以下结构先填在加载器自己的页中，退出固件后再交给内核。 */
#define GFX_INFO_PHYS 0x5000u
/* U3: memory map handoff (kernel/pmm.c pmm_apply_uefi_map reads it)
 *   0x5020: uint32 map_size / +0x04 desc_size / +0x08 desc_version / +0x0C data phys
 *   0x5100-0x5FFF: EFI_MEMORY_DESCRIPTOR array (truncated at 0x5FFF) */
#define UEFI_MMAP_PHYS     0x5100u
#define UEFI_MMAP_MAX      (0x6000u - 0x5100u)

typedef struct {
  UINT32 fb;
  UINT16 width, height;
  UINT8 bpp, fmt;
  UINT16 reserved;
  UINT32 stride, magic;
  UINT8 unused[12];
  UINT32 map_size, desc_size, desc_ver, map_phys;
  UINT8 pad[0xd0];
  UINT8 map[UEFI_MMAP_MAX];
} BOOT_INFO;

_Static_assert(sizeof(BOOT_INFO) == PAGE_BYTES, "one handoff page");
_Static_assert(offsetof(BOOT_INFO, magic) == 0x10, "UEFI magic offset");
_Static_assert(offsetof(BOOT_INFO, map_size) == 0x20, "UEFI map header offset");
_Static_assert(offsetof(BOOT_INFO, map) == 0x100, "UEFI map data offset");
#if defined(__x86_64__)
extern const UINT8 efi_x64_thunk[], efi_x64_thunk_end[];
typedef void (EFIAPI *X64_HANDOFF)(UINTN source, UINT32 size, UINTN info);
#endif

/* 简易内存拷贝（这里没有 libc） */
static void my_memcpy(volatile void *dst, const void *src, UINTN n) {
  volatile uint8_t *d = (volatile uint8_t *)dst;
  const uint8_t *s = (const uint8_t *)src;
  for (UINTN i = 0; i < n; ++i) d[i] = s[i];
}

static void capture_gop(EFI_BOOT_SERVICES *BS, BOOT_INFO *boot) {
  EFI_GRAPHICS_OUTPUT_PROTOCOL *gop = 0;
  EFI_STATUS st = BS->LocateProtocol(
      (EFI_GUID *)&gEfiGraphicsOutputProtocolGuid, 0, (void **)&gop);
  if (st == EFI_SUCCESS && gop != 0 && gop->Mode != 0 && gop->Mode->Info != 0) {
    int rgb565_idx = -1;
    UINT32 maxmode = gop->Mode->MaxMode;
    for (UINT32 i = 0; i < maxmode && i < 256; ++i) {
      UINTN sz = 0;
      EFI_GRAPHICS_OUTPUT_MODE_INFORMATION *info = 0;
      if (gop->QueryMode(gop, i, &sz, &info) == EFI_SUCCESS && info != 0) {
        serial_puts("EZEFI:mode ");
        serial_dec(i); serial_putc(' ');
        serial_dec(info->HorizontalResolution); serial_putc('x');
        serial_dec(info->VerticalResolution);
        serial_puts(" fmt="); serial_dec((uint32_t)info->PixelFormat);
        serial_puts(" stride="); serial_dec(info->PixelsPerScanLine);
        if (info->PixelFormat == PixelBitMask) {
          serial_puts(" mask=R="); serial_hex(info->PixelInformation.RedMask);
          serial_puts("/G="); serial_hex(info->PixelInformation.GreenMask);
          serial_puts("/B="); serial_hex(info->PixelInformation.BlueMask);
          if (info->PixelInformation.RedMask == 0xF800u &&
              info->PixelInformation.GreenMask == 0x07E0u &&
              info->PixelInformation.BlueMask == 0x001Fu) rgb565_idx = (int)i;
        }
        serial_puts("\r\n");
        ((EFI_FREE_POOL)BS->FreePool)(info);
      }
    }
    int use_rgb565 = 0;
    if (rgb565_idx >= 0 && gop->SetMode(gop, (UINT32)rgb565_idx) == EFI_SUCCESS)
      use_rgb565 = 1;

    EFI_GRAPHICS_OUTPUT_PROTOCOL_MODE *m = gop->Mode;
    EFI_GRAPHICS_OUTPUT_MODE_INFORMATION *info = m->Info;
    if (info == 0) return;
    uint32_t w   = info->HorizontalResolution;
    uint32_t h   = info->VerticalResolution;
    uint32_t fmt = (uint32_t)info->PixelFormat;
    uint32_t fb  = (uint32_t)(m->FrameBufferBase & 0xFFFFFFFFu);
    uint32_t stride = info->PixelsPerScanLine;
    uint8_t  bpp = use_rgb565 ? (uint8_t)16 : (uint8_t)32;

    serial_puts("EZEFI:gop ");
    serial_dec(w); serial_putc('x'); serial_dec(h);
    serial_puts(" fmt="); serial_dec(fmt);
    serial_puts(" fb="); serial_hex(fb);
    serial_puts(" stride="); serial_dec(stride);
    serial_puts("\r\n");

    if ((fmt < (uint32_t)PixelBitMask || use_rgb565) && w >= 320 && h >= 200 &&
        w <= 0xffffu && h <= 0xffffu && stride >= w &&
        m->FrameBufferBase >= LOW_KERNEL_END && m->FrameBufferBase < 0x100000000ull &&
        m->FrameBufferSize <= 0x100000000ull - m->FrameBufferBase &&
        (UINT64)stride * h * (bpp / 8u) <= m->FrameBufferSize) {
      boot->fb = fb;
      boot->width = (UINT16)w;
      boot->height = (UINT16)h;
      boot->bpp = bpp;
      boot->fmt = (UINT8)fmt;
      boot->stride = stride;
    } else {
      serial_puts("EZEFI:gop incompatible framebuffer; continuing without GOP\r\n");
    }
  } else {
    serial_puts("EZEFI:gop FAIL loc\r\n");
  }
}

static int valid_map(UINTN bytes, UINTN stride) {
  return stride >= sizeof(EFI_MEMORY_DESCRIPTOR) && stride <= UEFI_MMAP_MAX &&
         stride % 8 == 0 && bytes >= stride && bytes % stride == 0;
}

static int reclaims(const EFI_MEMORY_DESCRIPTOR *d) {
  return (d->Attribute & (1ull << 63)) == 0 &&
         ((d->Type >= EfiLoaderCode && d->Type <= EfiBootServicesData) ||
          d->Type == EfiConventionalMemory);
}

static UINT64 region_end(const EFI_MEMORY_DESCRIPTOR *d) {
  if (d->NumberOfPages > (UINT64_MAX - d->PhysicalStart) / PAGE_BYTES) return UINT64_MAX;
  return d->PhysicalStart + d->NumberOfPages * PAGE_BYTES;
}

/* Fixed kernel workspace cannot overwrite UEFI Runtime/ACPI/reserved ranges. */
static int range_in_map(const UINT8 *map, UINTN size, UINTN stride,
                        UINT32 start, UINT32 end, int require_reclaimable) {
  for (UINT64 at = start; at < end;) {
    UINT64 next = at;
    for (UINTN off = 0; off < size; off += stride) {
      const EFI_MEMORY_DESCRIPTOR *d = (const void *)(map + off);
      UINT64 limit = region_end(d);
      if (d->PhysicalStart <= at && at < limit) {
        if (require_reclaimable && !reclaims(d)) {
          serial_puts("EZEFI:reserved kernel workspace at ");
          serial_hex((UINT32)at);
          serial_puts(" end="); serial_hex((UINT32)limit);
          serial_puts(" type="); serial_dec(d->Type); serial_puts("\r\n");
          return 0;
        }
        next = limit;
        break;
      }
    }
    if (next == at) {
      serial_puts("EZEFI:kernel workspace not mapped at ");
      serial_hex((UINT32)at); serial_puts("\r\n");
      return 0;
    }
    at = next;
  }
  return 1;
}

/* Keep the low descriptors first when the fixed handoff page is too small.
 * Never silently omit entries the page allocator depends on. */
static int save_map(BOOT_INFO *boot, const UINT8 *map, UINTN size, UINTN stride, UINT32 ver) {
  UINTN copied = 0;
  for (int low = 1; low >= 0; --low) {
    for (UINTN off = 0; off < size; off += stride) {
      const EFI_MEMORY_DESCRIPTOR *d = (const void *)(map + off);
      if ((d->PhysicalStart < LOW_KERNEL_END) != low) continue;
      if (stride > sizeof(boot->map) - copied) {
        if (low) return 0;
        break;
      }
      my_memcpy(boot->map + copied, d, stride);
      copied += stride;
    }
  }
  boot->map_size = (UINT32)copied;
  boot->desc_size = (UINT32)stride;
  boot->desc_ver = ver;
  boot->map_phys = UEFI_MMAP_PHYS;
  return 1;
}

EFI_STATUS EFIAPI EfiMain(EFI_HANDLE ImageHandle, EFI_SYSTEM_TABLE *ST) {
  if (ST->ConOut) ST->ConOut->OutputString(ST->ConOut, (UINT16 *)msg16);
  serial_puts("EZEFI: hello\r\n");
#if defined(__x86_64__)
  serial_puts("EZEFI: x64 loader\r\n");
#endif
  EFI_BOOT_SERVICES *BS = ST->BootServices;
  EFI_STATUS st;
  int secure = secure_boot_state(ST);
  if (secure == 1) serial_puts("EZEFI: firmware SecureBoot=1\r\n");

  UINT64 fsize;
#if defined(EZOS_EMBED_KERNEL)
  fsize = (UINTN)embedded_kernel_end - (UINTN)embedded_kernel;
  if (fsize == 0 || fsize > KERNEL_SIZE_MAX) return EFI_LOAD_ERROR;
#else
  if (secure != 0) {
    static const CHAR16 message[] = {
      'S','e','c','u','r','e',' ','B','o','o','t',' ','r','e','q','u','i','r','e','s',' ',
      'a','n',' ','e','m','b','e','d','d','e','d',' ','k','e','r','n','e','l','.',13,10,0
    };
    if (ST->ConOut) ST->ConOut->OutputString(ST->ConOut, (CHAR16 *)message);
    serial_puts("EZEFI: Secure Boot requires an embedded kernel\r\n");
    return EFI_SECURITY_VIOLATION;
  }

  /* 1) 找到启动盘上的文件系统 */
  EFI_LOADED_IMAGE_PROTOCOL *li = 0;
  st = ((EFI_OPEN_PROTOCOL)BS->OpenProtocol)(ImageHandle,
      (EFI_GUID *)&gEfiLoadedImageProtocolGuid, (void **)&li,
      ImageHandle, 0, EFI_OPEN_PROTOCOL_GET_PROTOCOL);
  if (st != EFI_SUCCESS || li == 0) {
    serial_puts("EZEFI:kernel no loaded-image\r\n");
    return EFI_LOAD_ERROR;
  }
  EFI_SIMPLE_FILE_SYSTEM_PROTOCOL *fs = 0;
  st = ((EFI_OPEN_PROTOCOL)BS->OpenProtocol)(li->DeviceHandle,
      (EFI_GUID *)&gEfiSimpleFileSystemProtocolGuid, (void **)&fs,
      ImageHandle, 0, EFI_OPEN_PROTOCOL_GET_PROTOCOL);
  if (st != EFI_SUCCESS || fs == 0) {
    serial_puts("EZEFI:kernel no fs\r\n");
    return EFI_LOAD_ERROR;
  }
  EFI_FILE_PROTOCOL *root = 0;
  if (fs->OpenVolume(fs, &root) != EFI_SUCCESS || root == 0) {
    serial_puts("EZEFI:kernel no root\r\n");
    return EFI_LOAD_ERROR;
  }
  EFI_FILE_PROTOCOL *kf = 0;
  st = root->Open(root, &kf, (CHAR16 *)kernel_path, EFI_FILE_MODE_READ, 0);
  if (st != EFI_SUCCESS || kf == 0) {
    serial_puts("EZEFI:kernel open fail\r\n");
    root->Close(root);
    return EFI_LOAD_ERROR;
  }

  /* 2) 先读到临时缓冲，ExitBootServices 之后再搬到 KERNEL_LOAD（原因见下）*/
  /* real size: seek to EOF, read the position back. UEFI has no boot sector,
   * so the sector-count field boot.asm reads (0x1FC) does not exist here. */
  fsize = 0;
  st = ((EFI_FILE_SET_POSITION)kf->SetPosition)(kf, 0xFFFFFFFFFFFFFFFFull);
  if (st == EFI_SUCCESS)
    st = ((EFI_FILE_GET_POSITION)kf->GetPosition)(kf, &fsize);
  if (st != EFI_SUCCESS || fsize == 0 || fsize > (UINT64)KERNEL_SIZE_MAX) {
    serial_puts("EZEFI:kernel size fail st=0x");
    serial_hex((uint32_t)st);
    serial_puts(" size=");
    serial_dec((uint32_t)fsize);
    serial_puts("\r\n");
    kf->Close(kf);
    root->Close(root);
    return EFI_LOAD_ERROR;
  }
  st = ((EFI_FILE_SET_POSITION)kf->SetPosition)(kf, 0);
  if (st != EFI_SUCCESS) {
    serial_puts("EZEFI:kernel rewind fail\r\n");
    kf->Close(kf);
    root->Close(root);
    return EFI_LOAD_ERROR;
  }
#endif
  UINTN ksize = (UINTN)fsize;

  /* ★ 这里**不能**用 AllocatePages(AllocateAddress, KERNEL_LOAD) 预留。
   * OVMF 把自己的 DXE 堆放在 0x900000-0x1780000（9MB-23.6MB，type 4
   * EfiBootServicesData），内核的 KERNEL_LOAD(19MB) 正好落在里面 —— 固件
   * 只肯把 EfiConventionalMemory 交给你，于是 AllocateAddress 直接返回
   * EFI_NOT_FOUND(0x8000000E)，串口就停在 reserve fail。
   * 同理也不能在 ExitBootServices **之前** memcpy 过去：那段是固件正在用的
   * 堆，写进去固件自己就崩了。
   * 正确顺序：先用 AllocateMaxAddress 在 4 GiB 以下取暂存页读内核，等
   * ExitBootServices 之后（堆归我们了）再搬到 KERNEL_LOAD。 */
  UINTN kpages = (ksize + PAGE_BYTES - 1) / PAGE_BYTES;
  EFI_PHYSICAL_ADDRESS tmp = 0xffffffffu;
  st = ((EFI_ALLOCATE_PAGES)BS->AllocatePages)(AllocateMaxAddress, EfiLoaderData,
      kpages + 1, &tmp);
  if (st != EFI_SUCCESS || tmp < LOW_KERNEL_END ||
      tmp + (kpages + 1) * PAGE_BYTES > 0x100000000ull) {
    serial_puts("EZEFI:kernel alloc fail\r\n");
    if (st == EFI_SUCCESS) ((EFI_FREE_PAGES)BS->FreePages)(tmp, kpages + 1);
#if !defined(EZOS_EMBED_KERNEL)
    kf->Close(kf);
    root->Close(root);
#endif
    return EFI_LOAD_ERROR;
  }
  void *buf = (void *)(UINTN)tmp;
#if defined(EZOS_EMBED_KERNEL)
  my_memcpy(buf, embedded_kernel, ksize);
  serial_puts("EZEFI: using PE-embedded kernel\r\n");
#else
  UINTN read_size = ksize;
  st = kf->Read(kf, &read_size, buf);
  kf->Close(kf);
  root->Close(root);
  if (st != EFI_SUCCESS || read_size != ksize) {
    serial_puts("EZEFI:kernel read fail size=");
    serial_dec((uint32_t)read_size);
    serial_puts("\r\n");
    ((EFI_FREE_PAGES)BS->FreePages)(tmp, kpages + 1);
    return EFI_LOAD_ERROR;
  }
#endif
  serial_puts("EZEFI:kernel size=");
  serial_dec((uint32_t)ksize);
  serial_puts(" dst=");
  serial_hex((uint32_t)KERNEL_LOAD);
  serial_puts(" ok\r\n");

  BOOT_INFO *boot = (void *)(UINTN)(tmp + kpages * PAGE_BYTES);
  for (UINTN i = 0; i < sizeof(*boot); ++i) ((volatile UINT8 *)boot)[i] = 0;
  boot->magic = UEFI_MAGIC;
  capture_gop(BS, boot);

#if defined(__x86_64__)
  EFI_PHYSICAL_ADDRESS thunk = 0xffffffffu;
  UINTN code_size = (UINTN)efi_x64_thunk_end - (UINTN)efi_x64_thunk;
  if (code_size == 0 || code_size > PAGE_BYTES ||
      ((EFI_ALLOCATE_PAGES)BS->AllocatePages)(AllocateMaxAddress, EfiLoaderCode, 1, &thunk)
          != EFI_SUCCESS || thunk < LOW_KERNEL_END || thunk > 0xfffff000u) {
    serial_puts("EZEFI:x64 transition alloc fail\r\n");
    return EFI_LOAD_ERROR;
  }
  my_memcpy((void *)(UINTN)thunk, efi_x64_thunk, code_size);
#endif

  /* 4) 取内存映射（下面 5b 会拷一份交给内核） */
  UINTN map_size = 0, map_key = 0, desc_size = 0;
  UINT32 desc_ver = 0;
  void *mmap = 0;
  UINTN mmap_cap = 0;
  /* Allocating the buffer can itself change the map (new pool pages show up
   * as extra descriptors), so "ask for the size, allocate exactly that, then
   * fetch" is not enough - the second call can still come back
   * EFI_BUFFER_TOO_SMALL. Grow the slack and retry instead of giving up.
   * Seen with a 486912-byte kernel on OVMF i386: one round trip failed here
   * and the loader bailed out before ever jumping to the kernel. */
  for (int attempt = 0; attempt < 8; ++attempt) {
    UINTN need = 0;
    ((EFI_GET_MEMORY_MAP)BS->GetMemoryMap)(&need, 0, &map_key, &desc_size, &desc_ver);
    if (need == 0 || desc_size < sizeof(EFI_MEMORY_DESCRIPTOR) ||
        desc_size > UEFI_MMAP_MAX) break;
    UINTN slack = desc_size * (8 + 8 * (UINTN)attempt);
    if (need > UINTPTR_MAX - slack) break;
    UINTN cap = need + slack;
    void *tmp = 0;
    st = ((EFI_ALLOCATE_POOL)BS->AllocatePool)(EfiLoaderData, cap, &tmp);
    if (st != EFI_SUCCESS || tmp == 0) break;
    if (mmap) ((EFI_FREE_POOL)BS->FreePool)(mmap);
    mmap = tmp;
    mmap_cap = cap;
    UINTN in = cap;
    st = ((EFI_GET_MEMORY_MAP)BS->GetMemoryMap)(&in, mmap, &map_key, &desc_size, &desc_ver);
    if (st == EFI_SUCCESS) {
      map_size = in;
      break;
    }
  }
  if (st != EFI_SUCCESS || mmap == 0 || !valid_map(map_size, desc_size)) {
    serial_puts("EZEFI:map get fail\r\n");
    return EFI_LOAD_ERROR;
  }
  serial_puts("EZEFI:map n=");
  serial_dec((uint32_t)(map_size / desc_size));
  serial_puts(" key=0x"); serial_hex((uint32_t)map_key);
  serial_puts(" descsize="); serial_dec((uint32_t)desc_size);
  serial_puts("\r\n");

  /* 5) ExitBootServices（失败就用新的 MapKey 重试，最多 3 次） */
  int ebs_ok = 0;
  for (int attempt = 0; attempt < 3 && !ebs_ok; ++attempt) {
    if (!valid_map(map_size, desc_size) ||
        !range_in_map(mmap, map_size, desc_size, 0x5000u, 0x6000u, 1) ||
        !range_in_map(mmap, map_size, desc_size, 0x100000u, 0x800000u, 1) ||
        /* The unused 8–28 MiB gap can contain SMM/ACPI NVS. */
        !range_in_map(mmap, map_size, desc_size, KERNEL_LOAD, KERNEL_BSS_END, 1) ||
        !range_in_map(mmap, map_size, desc_size, PMM_START, LOW_KERNEL_END, 0) ||
        !save_map(boot, mmap, map_size, desc_size, desc_ver)) {
      serial_puts("EZEFI:firmware memory map conflicts with kernel workspace\r\n");
      return EFI_LOAD_ERROR;
    }
    st = ((EFI_EXIT_BOOT_SERVICES)BS->ExitBootServices)(ImageHandle, map_key);
    if (st == EFI_SUCCESS) {
      __asm__ __volatile__("cli" ::: "memory");
      ebs_ok = 1;
    } else {
      /* MapKey 可能已经变了，重新取一次 */
      /* U3: MapKey may have changed - re-fetch the FULL map into the buffer
       * so the copy handed to the kernel matches the post-EBS state */
      UINTN cap_in = mmap_cap;
      if (((EFI_GET_MEMORY_MAP)BS->GetMemoryMap)(&cap_in, mmap, &map_key, &desc_size, &desc_ver)
          != EFI_SUCCESS)
        break;
      map_size = cap_in;
    }
  }
  if (!ebs_ok) {
    serial_puts("EZEFI:ebs fail\r\n");
    return EFI_LOAD_ERROR;
  }
  serial_puts("EZEFI:ebs ok\r\n");

  serial_puts("EZEFI:mmap handoff size=");
  serial_dec((uint32_t)map_size);
  serial_puts(" copied=");
  serial_dec(boot->map_size);
  serial_puts("\r\n");

#if defined(__x86_64__)
  serial_puts("EZEFI:x64 entering i686\r\n");
  ((X64_HANDOFF)(UINTN)thunk)((UINTN)buf, (UINT32)ksize, (UINTN)boot);
#else
  my_memcpy((void *)GFX_INFO_PHYS, boot, sizeof(*boot));
  if (boot->fb) serial_puts("EZEFI:gop write@0x5000 ok\r\n");
  serial_puts("EZEFI:uefi magic@0x5010 ok\r\n");

  /* 5c) ExitBootServices 之后才往 KERNEL_LOAD 搬：那片是固件刚交出来的
   * DXE 堆（见第 2 步注释）。 */
  my_memcpy((void *)(UINTN)KERNEL_LOAD, buf, ksize);
  serial_puts("EZEFI:kernel copied to ");
  serial_hex((uint32_t)KERNEL_LOAD);
  serial_puts("\r\n");

  /* 6) 跳到内核入口；内核接管分页和 GDT，而不是沿用固件的状态。 */
  typedef void (*entry_t)(void);
  ((entry_t)KERNEL_LOAD)();
#endif

  /* Boot services are gone; never return into the firmware. */
  for (;;) __asm__ __volatile__("cli; hlt");
}
