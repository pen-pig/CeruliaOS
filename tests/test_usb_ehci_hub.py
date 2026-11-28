"""High-speed hub class control and unavailable downstream TT on stock QEMU.

usb-redir exposes a hub descriptor and port registers, not a real USB tree.
This does NOT prove delivery of interrupts from behind a physical translator.
"""
import argparse
from pathlib import Path
import struct
from test_boot_compat import ROOT, boot, ovmf_x64_fd, type_on_usb, wait_for
from test_usb_composite import Combo


class Hub(Combo):
    def __init__(self):
        self.requests = []
        self.enabled = set()
        super().__init__(False, high=True)
        self.dev = bytes.fromhex("12 01 00 02 09 00 01 40 27 06 34 12 00 01 00 00 00 01")
        self.config = bytes.fromhex("09 02 19 00 01 01 00 e0 00 09 04 00 00 01 09 00 00 00 "
                                    "07 05 81 03 01 00 0c")

    def announce(self):
        self.enabled.clear()
        tail = lambda vals: bytes(vals).ljust(32, b"\0")
        self.send(4, struct.pack("<I", 1)+tail([0])+tail([9])+tail([0])+tail([0]))
        kinds = [255]*32
        mps = [0]*32
        kinds[0] = kinds[16] = 0
        mps[0] = mps[16] = 64
        kinds[17], mps[17] = 3, 1
        self.send(5, bytes(kinds)+bytes(17)+bytes([12])+bytes(14)+bytes(32)+struct.pack("<32H", *mps))
        self.send(1, struct.pack("<BBBBHHH", 2, 9, 0, 1, 0x627, 0x1234, 0x100))
        self.ready.set()

    def control(self, req, typ, value, index, nbytes, data):
        self.requests.append((typ, req, value, index))
        if (typ, req, value) == (0xa0, 6, 0x2900):
            reply = bytes.fromhex("09 29 02 01 00 01 00 00 ff")[:nbytes]
            return 0, len(reply), reply
        if (typ, req) == (0xa3, 0) and index in (1, 2):
            status = 0x101 | (0x200 if index == 1 else 0) | (2 if index in self.enabled else 0)
            return 0, 4, struct.pack("<HH", status, 0)
        if typ == 0x23:
            if req == 3 and value == 4:
                self.enabled.add(index)
            if req == 1 and value == 1:
                self.enabled.discard(index)
            if req in (1, 3, 8):
                return 0, 0, b""
        return super().control(req, typ, value, index, nbytes, data)


def run(image, baseline):
    hub = Hub()

    def guest(q, s, _):
        if baseline:
            # OVMF also sends hub descriptor/power requests before the OS.
            assert "USB-HID: USB 2.0 hub ready" not in s.snapshot()
            assert not any(r[:2] == (0x23,8) for r in hub.requests), hub.requests
            print("BASELINE: Cerulia did not claim or service the high-speed hub")
            return
        assert "USB-HID: USB 2.0 hub ready" in s.snapshot()
        for port in (1, 2):
            assert (0x23, 3, 8, port) in hub.requests  # powered
            assert (0x23, 3, 4, port) in hub.requests   # reset
            assert (0x23, 1, 1, port) in hub.requests   # disable missing device
        assert {(0x23, 8, 0, 1), (0x23, 8, 0x8000, 1)} <= set(hub.requests)
        assert "USB-HID: attached via hub" not in s.snapshot()
        start = s.size()
        hub.send(2)
        wait_for(lambda:s.tail_from(start), "USB-HID: detached EHCI root port")
        start = s.size()
        hub.announce()
        wait_for(lambda:s.tail_from(start), "USB-HID: attached EHCI root port")
        wait_for(lambda:str(hub.requests.count((0x23, 1, 1, 2))), "2")
        start = s.size()
        type_on_usb(q, s.snapshot, "ver", "] > ", lambda _:"Cerulia Kernel version" in s.tail_from(start))
        print("PASS EHCI hub control, failed TT cleanup and root hotplug; direct HID still works")

    try:
        boot("synthetic high-speed EHCI hub", ["-machine","q35,i8042=off","-m","256",
             "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),
             "-device","usb-ehci,id=fast","-device","usb-kbd,bus=fast.0,port=2",
             "-chardev",f"socket,id=hubwire,host=127.0.0.1,port={hub.port}",
             "-device","usb-redir,bus=fast.0,port=1,chardev=hubwire"],
             ["Keyboard: using USB HID"],image=image,usb_boot=True,usb_keyboard=True,after=guest)
        if hub.error:
            raise hub.error
    finally:
        hub.close()


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image",type=Path,default=ROOT/"usb-image.bin")
    p.add_argument("--baseline",action="store_true")
    args=p.parse_args()
    run(args.image,args.baseline)
