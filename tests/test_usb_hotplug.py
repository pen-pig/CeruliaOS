"""USB device attach/re-attach while the guest is already running."""
import argparse
import time
from pathlib import Path
from test_boot_compat import ROOT, boot, ovmf_x64_fd, wait_for, type_on_usb

def run(image, baseline, uhci, cold):
    host=["-device","piix3-usb-uhci,id=hid"] if uhci else []
    bus,port=("hid.0","1") if uhci else ("usb.0","4")
    controller="UHCI" if uhci else "xHCI"
    up="USB-HID: attached "+controller+" root port"
    down="USB-HID: detached "+controller+" root port"
    def guest(q,s,tmp):
        def attach():
            r=q.cmd("device_add",driver="usb-kbd",id="second",bus=bus,port=port)
            assert "return" in r,r
        def detach(name):
            r=q.cmd("device_del",id=name);assert "return" in r,r
        def expect(text,start=0):
            return wait_for(lambda:s.tail_from(start),text)
        start=s.size()
        if not cold:
            detach("first")
            if not baseline:expect(down,start)
            else:time.sleep(.6)
        attach()
        if baseline:
            time.sleep(2)
            assert "USB-HID: attached" not in s.tail_from(start)
            print("BASELINE second keyboard was not enumerated")
            return
        expect(up,start)
        for i in range(9): # exceed the eight xHCI hardware context buffers
            start=s.size()
            type_on_usb(q,s.snapshot,"ver","] > ",
                        lambda _:"Cerulia Kernel version" in s.tail_from(start))
            if i==8:break
            start=s.size()
            detach("second")
            expect(down,start)
            attach()
            expect(up,start)
        print(f"PASS {controller} hot attach/re-attach and queue/slot recycling",flush=True)
    keyboard=[] if cold else ["-device",f"usb-kbd,bus={bus},id=first,port={port}"]
    boot(controller+" root HID hotplug",["-machine","q35,i8042=off","-cpu","max","-m","256",
         "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),
         *host,*keyboard],
         ["COM1 rescue input active" if cold else "Keyboard: using USB HID"],
         image=image,usb_boot=True,usb_keyboard=not cold,rescue=cold,after=guest)

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--image",type=Path,default=ROOT/"usb-image.bin")
    p.add_argument("--uhci",action="store_true")
    p.add_argument("--cold",action="store_true")
    p.add_argument("--baseline",action="store_true");a=p.parse_args();run(a.image,a.baseline,a.uhci,a.cold)
