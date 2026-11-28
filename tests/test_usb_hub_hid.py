"""USB keyboard and pointer behind one or two USB2 hubs."""
import argparse
from pathlib import Path
import time
from test_boot_compat import ROOT, boot, ovmf_x64_fd, type_on_usb, wait_for

def after(q,s,tmp,bus,port):
    wait_for(s.snapshot,"USB-HID: USB 2.0 hub ready")
    wait_for(s.snapshot,"USB-MOU: boot mouse")
    start=s.size()
    # Accept any observed motion, including the first report. Allow for
    # QEMU HID activation and diagnostic rate limiting under parallel runs.
    for _ in range(10):
        assert "return" in q.cmd("input-send-event",events=[
            {"type":"rel","data":{"axis":"x","value":17}},
            {"type":"rel","data":{"axis":"y","value":-9}}])
        time.sleep(.48)
        if "USB-MOU: move dx=17 dy=-9" in s.tail_from(start):break
    else:raise AssertionError(s.tail_from(start))

    start=s.size()
    assert "return" in q.cmd("device_del",id="kbd")
    wait_for(lambda:s.tail_from(start),"USB-HID: detached via hub")
    start=s.size()
    assert "return" in q.cmd("device_add",driver="usb-kbd",id="kbd2",bus=bus,port=port+".1")
    wait_for(lambda:s.tail_from(start),"USB-HID: attached via hub")
    start=s.size()
    type_on_usb(q,s.snapshot,"ver","] > ",lambda _:"Cerulia Kernel version" in s.tail_from(start))
    print("PASS keyboard, mouse and keyboard hotplug behind the same USB 2.0 hub",flush=True)

def nested_after(q,s,tmp,bus,port):
    after(q,s,tmp,bus,port)
    start=s.size()
    assert "return" in q.cmd("device_del",id="dock")
    wait_for(lambda:s.tail_from(start),"USB-HID: detached via hub")
    assert "return" in q.cmd("device_add",driver="usb-hub",id="dock2",bus=bus,port=port)
    assert "return" in q.cmd("device_add",driver="usb-kbd",id="kbd3",bus=bus,port=port+".1")
    assert "return" in q.cmd("device_add",driver="usb-mouse",id="mou2",bus=bus,port=port+".2")
    wait_for(lambda:s.tail_from(start),lambda t:t.count("USB-HID: attached via hub")>=3)
    start=s.size()
    type_on_usb(q,s.snapshot,"ver","] > ",lambda _:"Cerulia Kernel version" in s.tail_from(start))
    print("PASS inner dock and both child slots retired; fresh dock keyboard works")

if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--uhci",action="store_true")
    parser.add_argument("--ohci",action="store_true")
    parser.add_argument("--nested",action="store_true")
    parser.add_argument("--baseline",action="store_true")
    parser.add_argument("--image",type=Path,default=ROOT/"usb-image.bin")
    args=parser.parse_args();uhci,ohci=args.uhci,args.ohci
    if args.nested and (uhci or ohci):parser.error("--nested requires xHCI")
    host=["-device","piix3-usb-uhci,id=hid"] if uhci else []
    if ohci:host=["-device","pci-ohci,id=hid"]
    bus,port=("hid.0","1") if uhci or ohci else ("usb.0","3")
    inner=port+".1" if args.nested else port
    dock=["-device",f"usb-hub,bus={bus},port={inner},id=dock"] if args.nested else []
    boot(("OHCI" if ohci else "UHCI" if uhci else "xHCI")+" USB2 Hub and HID children",
         ["-machine","q35,i8042=off","-cpu","max","-m","256",
         "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),*host,
         "-device",f"usb-hub,bus={bus},port={port},id=hub",
         *dock,"-device",f"usb-kbd,bus={bus},port={inner}.1,id=kbd",
         "-device",f"usb-mouse,bus={bus},port={inner}.2,id=mou"],
         ["EZEFI:x64 entering i686"]+(["COM1 rescue input active"] if args.baseline else []),
         image=args.image,usb_boot=True,usb_keyboard=not args.baseline,rescue=args.baseline,
         after=None if args.baseline else lambda q,s,tmp:(nested_after if args.nested else after)(q,s,tmp,bus,inner))
