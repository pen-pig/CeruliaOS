"""test_usbmsc_rules.py - USB-MSC 块设备接受判据的独立复算闸门

为什么需要它：QEMU 上挂的是标准 512 字节扇区的 USB 盘，所以
"设备报 4096 字节扇区"和"last LBA 是 0xFFFFFFFF 哨兵"这两条路径
在 E2E 里**永远走不到** —— 修了也不会有人发现。真正的风险恰恰在这
两条静默错路上：4K 扇区盘会按 8 倍差的 LBA 去读写别的扇区还报成功，
容量未知盘会被回绕成"1 个扇区"。所以判据必须能脱离 QEMU 验证。

这一端是独立复算，kernel/usbmsc.c 里scsi_read_capacity 是被验的那一端。
两边靠"判据写在这里，要改就一起改"维持一致。
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MSC_C = os.path.join(ROOT, "kernel", "usbmsc.c")


def msc_accept(last_lba, blksize):
    """READ CAPACITY(10) 的结果是否可被注册为块设备。

    两条拒绝规则，各自对应内核里的 fail-closed：
      1. last LBA == 0xFFFFFFFF 是规范规定的"容量未知"哨兵。
         `last + 1u` 会回绕成 0，而调用方那句 `if (sectors < 1u)
         sectors = 1u` 会把容量未知的盘注册成 1 扇区的盘。
      2. 块大小必须正好 512。读写全走 READ(10)/WRITE(10)，数据阶段
         固定 512 字节；按 4K 算出来的 LBA 和实际差 8 倍。
    """
    if last_lba == 0xFFFFFFFF:
        return False
    if blksize != 512:
        return False
    return True


CASES = [
    # (last_lba, blksize, expect, 为什么)
    (0x000FFFFF, 512, True, "普通 1G 盘，必须接受"),
    (0x00000000, 512, True, "单扇区盘，合法"),
    (0xFFFFFFFF, 512, False, "容量未知哨兵：原会回绕成 0 -> 注册成 1 扇区盘"),
    (0x000FFFFF, 4096, False, "4K 扇区盘：原会照常注册 -> LBA 差 8 倍，读写坏扇区"),
    (0x000FFFFF, 1024, False, "1K 扇区：同理不支持"),
    (0x000FFFFF, 0, False, "块大小为 0：原会被补成 512 后照常注册"),
    (0xFFFFFFFF, 4096, False, "两条规则同时触发"),
]


def _code_lines(text):
    """去掉注释，只留可执行代码行。

    必须这么做的原因：闸门自己被"注释里也写了同样的字符串"骗过 ——
    用 find() 在整段文本里搜，命中的是注释而非代码，于是"哨兵检查必须排在
    last+1u 之前"这条顺序判据永远为真，抓不到任何回归。
    """
    out = []
    for ln in text.splitlines():
        s2 = ln.split("//")[0]
        s2 = s2.split("/*")[0]
        if s2.strip():
            out.append(s2)
    return out


def check_source_agrees():
    """内核那一边必须真的带这两条拒绝，不能只改了这个副本。

    没这一步的话，这个文件完全可以在内核没改的情况下"全绿" ——
    独立复算只证明了复算自身自洽。两边要靠**读内核源码**钉在一起。
    """
    bad = 0
    try:
        with io.open(MSC_C, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        print("FAIL:读不到 kernel/usbmsc.c: %s" % e)
        return 1

    body = src[src.index("scsi_read_capacity"):]
    body = body[:body.index("static int scsi_read10")] if         "static int scsi_read10" in body else body
    code = _code_lines(body)

    i_guard = i_bs = i_sum = -1
    for k, ln in enumerate(code):
        if i_guard < 0 and "0xFFFFFFFFu" in ln and "last" in ln:
            i_guard = k
        if i_bs < 0 and re.search(r"bs\s*!=\s*512u", ln):
            i_bs = k
        if i_sum < 0 and "last + 1u" in ln:
            i_sum = k

    # 1) 容量未知哨兵
    if i_guard < 0:
        print("FAIL: usbmsc.c 的 scsi_read_capacity 没有拒绝 last==0xFFFFFFFF"
              "（容量未知哨兵会被 last+1u 回绕成 0）")
        bad += 1
    # 2) 块大小必须 512
    if i_bs < 0:
        print("FAIL: usbmsc.c 的 scsi_read_capacity 没有 `bs != 512u` 拒绝"
              "（4K 扇区设备会按 8 倍差的 LBA 读写）")
        bad += 1
    # 3) 拒绝必须发生在 last+1u 之前（否则回绕已经发生了）
    if i_guard >= 0 and i_sum >= 0 and i_guard > i_sum:
        print("FAIL: 哨兵检查必须排在 `last + 1u` 之前，否则回绕已经发生")
        bad += 1
    if i_bs >= 0 and i_sum >= 0 and i_bs > i_sum:
        print("FAIL: 块大小检查必须排在 `last + 1u` 之前")
        bad += 1
    return bad


def main():
    bad = check_source_agrees()
    if not bad:
        print("OK   kernel/usbmsc.c 的拒绝规则与本表一致")
        print("")
    for last, bs, want, why in CASES:
        got = msc_accept(last, bs)
        flag = "OK  " if got == want else "FAIL"
        if got != want:
            bad += 1
        print("%s accept=%-5s want=%-5s last=0x%08X bs=%-5d | %s"
              % (flag, got, want, last, bs, why))
    print("\n%d/%d" % (len(CASES) - bad, len(CASES)))
    if bad:
        print("FAIL：接受判据与预期不符")
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
