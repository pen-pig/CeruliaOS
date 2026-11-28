"""Keyboard only on the second UHCI/EHCI controller; mouse on either."""
import argparse
from pathlib import Path
from test_boot_compat import ROOT, boot, ovmf_x64_fd, wait_for
from test_usb_ehci_hid import check_keys, mouse

def run(kind, image, baseline, split):
    controller="piix3-usb-uhci" if kind=="UHCI" else "usb-ehci"
    target="a.0" if split else "b.0"
    port=1 if split else 2
    hw=["-machine","q35,i8042=off","-m","256",
        "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),
        "-device",controller+",id=a","-device",controller+",id=b",
        "-device","usb-kbd,bus=b.0,port=1,id=kbd",
        "-device",f"usb-mouse,bus={target},port={port}"]
    def guest(q,s,_):
        if baseline:
            assert f"{kind} controller(s) found, initialized " in s.snapshot()
            print("BASELINE: keyboard on second controller unavailable; serial rescue required")
            return
        mouse(q,s)
        start=s.size()
        assert "return" in q.cmd("device_del",id="kbd")
        wait_for(lambda:s.tail_from(start),f"USB-HID: detached {kind} root port")
        assert "return" in q.cmd("device_add",driver="usb-kbd",bus="b.0",port="1",id="again")
        wait_for(lambda:s.tail_from(start),f"USB-HID: attached {kind} root port")
        check_keys(q,s)
        print(f"PASS second {kind} controller keyboard replug and USB mouse movement")
    expected=["COM1 rescue input active"] if baseline else [
        f"{kind} controller(s) found, initialized 2","Keyboard: using USB HID"]
    boot("two "+kind+" controllers",hw,expected,image=image,
         usb_boot=True,rescue=baseline,usb_keyboard=not baseline,after=guest)

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kind",choices=("UHCI","EHCI"))
    p.add_argument("--image",type=Path,default=ROOT/"usb-image.bin")
    p.add_argument("--baseline",action="store_true")
    p.add_argument("--split",action="store_true")
    a=p.parse_args()
    for kind in ((a.kind,) if a.kind else ("UHCI","EHCI")):
        run(kind,a.image,a.baseline,a.split or not a.kind)
