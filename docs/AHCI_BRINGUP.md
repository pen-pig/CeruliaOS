# AHCI/SATA 真机 bring-up 检查单（H1b）

> 适用对象：要把 EZOS 的 AHCI 驱动（`kernel/ahci.c`）往**真实 PC** 上点的人。
> 场景：**无屏**，只靠串口（COM1，115200 8N1）判断卡在哪一步。
> 配套：`docs/USB_BOOT.md`（镜像制作 + BIOS 设置总清单）。
>
> **2026-10-18 审查后状态**：代码侧已修为"可上真机"状态（修了 3 处 QEMU
> 看不见的问题，见第 2 节），QEMU 侧 `tests/test_ahci.py` 10/10 保持绿。
> **真机验证尚未做**——需要一台 SATA 模式设为 AHCI 的机器 + 一块 SATA 盘。

---

## 0. 一句话结论

BIOS 里 **SATA 模式设为 AHCI**（不是 IDE/RAID/兼容模式）→ U 盘启动 →
串口里看到 `AHCI: port 0 online (model: ...)` → shell 里
`setdrive 4` + `ls` 挂上盘。全程只需串口，不需要屏幕。

---

## 1. 上机前：BIOS 设置

| 设置 | 应设为 | 说明 |
|---|---|---|
| **SATA 模式** | **AHCI** | 内核扫 PCI class `0x0106` 找 AHCI 控制器。IDE/兼容模式下 class 变成 `0x0101`，驱动找不到控制器（串口只打 `AHCI: no SATA/AHCI controller found, skipping`），此时只能走 `setdrive 0-3` 的 ATA PIO 路径。RAID 模式同理不支持。 |
| CSM / Legacy Boot | Enabled | 见 `docs/USB_BOOT.md` 第 3 节。 |
| Secure Boot | Disabled | 同上。 |

> 拿不准就先只改 SATA 模式这一项，其他保持默认。

---

## 2. 本次审查修掉的三处"QEMU 全绿、真机必坏"（2026-10-18）

审查方法：逐行对照 Intel AHCI 1.0/1.3.1 规范 + QEMU 源码
（`hw/ide/ahci.c`、`hw/ide/ahci-internal.h`），区分"QEMU 替硬件兜住的底"
和"真机 HBA 真的会做的事"。三处都属前者：

| # | 问题 | QEMU 为什么不红 | 真机上会怎样 | 修法 |
|---|---|---|---|---|
| 1 | 命令头 **W（写）位误用 bit 7**（那是 P 预取位，W 在 bit 6） | QEMU 对 DMA 命令按 FIS 里的**命令字**定向（`core.c` 的 `IDE_DMA_WRITE` 分派），根本不读命令头 W 位；E2E 的 write+cat 全程绿 | 真机 HBA 按 W 位编程总线主控 DMA 方向。写命令 W=0 被当成"设备→内存"：数据永远到不了盘，或协议错误超时 | 改用 `CH_WRITE` 宏（bit 6），并在 `tools/lint.py` 加文件级 ERROR 闸门：命令头 DW0 出现裸位写法即红（已变异测试证伪：改回 `(1u<<7)` → 2 errors） |
| 2 | **BOHC 所有权交接写错位**：写了 `boqc\|1`（又写一遍 BOS）却等 bit 1（OOS） | QEMU 未实现 BOHC，读回 0，这条路径从不进入 | 真机 BIOS 从 SATA 启动时 BOS 恒为 1 → 每次开机白等满 1 秒，且所有权根本拿不到（规范：OOS=1 且 BOS=0 才算拿到）。控制器可能拒绝命令 | 按规范 10.6：置 OOS（bit 1）→ 等 `OOS=1 && BOS=0` → 超时带 `BOHC=0x..` 日志继续。lint 第二道闸门：写 `GHC_BOQC` 不带 `BOHC_OOS` 即红 |
| 3 | **ABAR 页映射 uint32 回绕**：`page_pa + 3*4096` 在 ABAR 落在 `0xFFFFF000` 一带时回绕成 `0x2EF0`，把低地址页映射掉、顶端寄存器区反而没映射；且 `pa=0`（BAR5 没编程）不设防，会映射掉 IVT 页读垃圾"CAP" | QEMU 的 ich9-ahci 把 ABAR 放在 `0xFEBF1000`，离 4GB 顶端远，回绕不发生 | ABAR 在 `0xFExxF000` 是真机常见位置（PCI 内存区自上向下分配）。轻则映射错页读到垃圾寄存器、表现为"控制器假上线、端口全 absent"；重则缺页异常 | 页号改 64 位算 + `>4GB` 不映射 + `pa==0` fail closed |

另：端口探测时显式 `PxIE=0`（轮询模式不需要端口中断；防 BIOS 留着的 PxIE
位在个别控制器上照拉中断线，撞进没有处理函数的 IRQ 向量）。

**已知未做（有意留下）**：HBA reset（`GHC.HR`）保持关闭——端口级
stop + COMRESET 已够用，且 reset 在部分控制器上会挂。真机端口起不来时，
它作为第一诊断手段打开（`kernel/ahci.c` 里的 `#if 0` 块）。

---

## 3. 串口诊断速查（上电后依次应该看到）

正常路径（值以 QEMU 实测为例，真机地址/型号不同）：

```
pci: scanned bus, 7 device(s) found
ahci: abar pa=0xfebf1000                        <- ABAR 物理地址（真机常在 0xFExx....）
ahci: ghc pg phys=0xfebf1000 ok                 <- 页表映射自检（UNMAPPED = 映射失败）
ahci: ssts pg phys=0xfebf1000 ok
ahci: reread cap=0xc0141f05                     <- 重读 CAP 确认 MMIO 真的通
ahci: sig@0x124=0x00000101                      <- port0 PxSIG：ATA 直连盘
ahci: ssts@0x128=0x00000113                     <- DET=3（设备在位且链路协商完成）
ahci: cap=0x...                                  <- 控制器能力位
ahci: pi =0x...                                  <- 已实现端口位图
ahci: nports=6
ahci: controller 8086:2922 ver 1.3 ports=6 impl=1
ahci: enter probe port 0 (PI set)
ahci: port 0 online (model: QEMU HARDDISK ...)  <- 探测成功：IDENTIFY 读通、型号拿到
ahci: first read ok                             <- 首次 DMA 读成功
ahci: first write ok                            <- 首次 DMA 写成功
ahci: 1 port(s) online
```

每一行的含义和异常值的处置见第 5 节症状表。

---

## 4. 检查单（按顺序走，每步都有串口判据）

- [ ] **1. BIOS**：SATA 模式 = AHCI；CSM 开；Secure Boot 关。
- [ ] **2. 接线**：USB-TTL 接主板 COM1（针脚见 `docs/USB_BOOT.md` 第 5 节），
      115200 8N1。无屏也可以完成全部判断。
- [ ] **3. 启动盘**：`usb-image.bin` 整盘写 U 盘（`tools/make_usb_boot.py`），
      **不要**在 Windows 里点"格式化"。
- [ ] **4. 上电**：串口出现 `pci: scanned bus` → `ahci: abar pa=0x...`。
      - 没有 `ahci:` 任何行 → 控制器没找到，回第 1 步查 SATA 模式。
- [ ] **5. 看映射自检**：`ghc pg phys=... ok` / `ssts pg phys=... ok`。
      出现 `UNMAPPED` → 页表映射失败（驱动会 abort，见症状表）。
- [ ] **6. 看端口探测**：`enter probe port N` → `port N online (model: ...)`。
      - 插了盘却 `absent (SSTS.DET != 3)` → 数据线/电源或模式问题。
      - `is ATAPI (unsupported, skip)` → 光驱，正常跳过。
- [ ] **7. 挂盘**：shell 里 `setdrive 4`（drive 4 = AHCI port 0，
      端口 N → drive 4+N），然后 `ls`。
      - `setdrive 5` 对应 port 1，以此类推（AHCI_MAX_PORTS=8）。
- [ ] **8. 读写验证**：`format exfat` → `write a.txt hello` → `cat a.txt`
      → 应回显 `hello`。这一步同时验证读和写两条 DMA 路径。
- [ ] **9. 容量核对**：`ls -l` 或 format 后提示的扇区数，和盘标称容量对得上
      （偏差在 0.1% 内正常；差一半说明读到的是另一块盘/另一个端口）。

---

## 5. 症状 → 原因 → 处置表

| 串口/现象 | 最可能原因 | 处置 |
|---|---|---|
| 完全没有 `ahci:` 行 | SATA 模式是 IDE/RAID，或真没有 AHCI 控制器 | BIOS 改 SATA 模式为 AHCI；老主板只有 IDE → 用 `setdrive 0-3` |
| `BAR5 is not a memory BAR` / `BAR5 not programmed (pa=0)` | 拿到的不是 AHCI 设备，或固件没编程 BAR5 | 换已知 AHCI 控制器；IDE 模式下 class 不符也会走到这 |
| `ABAR page map failed at 0x...` | 页表分配失败（PMM 耗尽）或地址异常 | 看 `pmm` 相关 klog；一般是内存布局问题不是 AHCI |
| `BIOS holds controller (BOHC=0x1), requesting` 后跟 `OS ownership acquired` | 正常握手（真机常见） | 无需处置 |
| `BOQC handoff timeout, BOHC=0x..` | BIOS 不响应 OOS（规范允许，BIOS 实现良莠不齐） | 带日志继续，端口探测照常；若随后端口全 absent 才需要处理（试着手动开 HBA reset） |
| `port N absent (SSTS.DET != 3)` | 口上没设备、线没插、或设备没上电 | 换线/换口/换盘；注意 20ms 判定空口是快路径，插着盘不会误判 |
| `port N bad signature, skip` | 签名 FIS 异常（端口倍增器/SEMB 也会落这） | 直连硬盘应为 `0x00000101`；排查线缆 |
| `IDENTIFY failed` + `TFD=/IS=/SERR=/CI=` 四行 | DMA 读路径坏 | 这四行就是 bring-up 的现场：TFD 带 ERR、SERR 带协议错，按值查 SATA 协议 |
| `online` 但 `setdrive 4` 后 `ls` 挂不上 | 容量不可信返回 0（fail closed）或盘上无 FS | 先 `format exfat` 再 `ls`；`fs` 卷大小与盘标称差太多 → 报容量行 |
| 写操作"成功"但数据回读不对/重启后丢 | —— 本次已修（W 位 bug 的典型症状） | 若还出现，确认跑的是含 `CH_WRITE` 修复的内核（lint 闸门会拦回退） |
| 随机重启/花屏后键盘无响应 | 见 `docs/USB_BOOT.md` 排障表 | 与本驱动无关的已知问题另行排查 |

---

## 6. 已知限制（不是缺陷，先知道）

- **单控制器**：`ahci_init` 取扫描到的第一个 AHCI 控制器，多控制器机器只有第一个生效。
- **不接 IRQ**：全程轮询（CI 位 + `g_pit_ticks` 真实时间超时），与网络栈一致。
- **不支持**：ATAPI（光驱，正常跳过）、端口倍增器（PMP，签名不符跳过）、NCQ（单槽轮询）。
- **容量上限**：块层 LBA 是 32 位，>2TB 的部分截断（fail safe，不溢出）。
- **HBA reset 关闭**：见第 2 节末尾。
