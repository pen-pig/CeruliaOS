"""Exercise a PCI OHCI controller as the only keyboard/mouse path."""
import argparse
from pathlib import Path
import time
from test_boot_compat import ROOT, boot, ovmf_x64_fd, wait_for, type_on_usb

def plug_keyboard(q,s,device_id,companion):
    start=s.size()
    props={"driver":"usb-kbd","id":device_id,
           "bus":"fast.0" if companion else "hid.0","port":"1"}
    if companion: props["usb_version"]=1
    assert "return" in q.cmd("device_add",**props)
    if companion: wait_for(lambda:s.tail_from(start),"released to companion")
    wait_for(lambda:s.tail_from(start),"USB-HID: attached OHCI root port")
    start=s.size()
    type_on_usb(q,s.snapshot,"ver","] > ",lambda _:"Cerulia Kernel version" in s.tail_from(start))

def guest(q,s,tmp,companion=False):
    start=s.size()
    for _ in range(10):
        r=q.cmd("input-send-event",events=[
            {"type":"rel","data":{"axis":"x","value":21}},
            {"type":"rel","data":{"axis":"y","value":-13}}])
        assert "return" in r,r
        time.sleep(.42)
        if "USB-MOU: move dx=21 dy=-13" in s.tail_from(start):break
    else:raise AssertionError(s.tail_from(start))
    print("PASS OHCI mouse delivered relative motion",flush=True)
    start=s.size()
    assert "return" in q.cmd("device_del",id="kbd")
    wait_for(lambda:s.tail_from(start),"USB-HID: detached OHCI root port")
    plug_keyboard(q,s,"replug",companion)
    print("PASS OHCI keyboard detach and reattach",flush=True)

def run(image,baseline,split,cold,companion):
    second=["-device","pci-ohci,id=second"] if split else []
    pointer_bus="second.0,port=1" if split else "hid.0,port=2"
    def cold_guest(q,s,tmp):
        plug_keyboard(q,s,"kbd",companion)
        print("PASS OHCI was empty at boot and accepted a USB keyboard after login",flush=True)
    controller=["-device","pci-ohci,id=hid"]
    if companion:
        controller=["-device","ich9-usb-ehci1,id=fast","-device","pci-ohci,id=hid,masterbus=fast.0,num-ports=3"]
    devices=[] if cold else ["-device","usb-kbd,bus=hid.0,port=1,id=kbd",
                             "-device","usb-mouse,bus="+pointer_bus+",id=mouse"]
    if companion and not cold:
        devices=["-device","usb-kbd,usb_version=1,bus=fast.0,port=1,id=kbd",
                 "-device","usb-mouse,usb_version=1,bus=fast.0,port=2,id=mouse"]
    expected=["OHCI: controller ready"]
    if baseline: expected=[]
    if baseline or cold: expected.append("COM1 rescue input active")
    else:
        expected.append("Keyboard: using USB HID")
        if companion: expected.append("released to companion")
    boot("OHCI-only input",["-machine","q35,i8042=off","-m","256",
         "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),
         *controller,*second,*devices],
         expected,
         image=image,usb_boot=True,usb_keyboard=not (baseline or cold),rescue=baseline or cold,
         after=(lambda *_:print("BASELINE OHCI keyboard unavailable; serial rescue required",flush=True)) if baseline else cold_guest if cold else lambda q,s,tmp:guest(q,s,tmp,companion))

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--image",type=Path,default=ROOT/"usb-image.bin")
    p.add_argument("--baseline",action="store_true")
    p.add_argument("--split-controllers",action="store_true")
    p.add_argument("--cold",action="store_true")
    p.add_argument("--companion",action="store_true")
    a=p.parse_args()
    if a.split_controllers and a.companion:
        p.error("--split-controllers and --companion cannot be combined")
    run(a.image,a.baseline,a.split_controllers,a.cold,a.companion)
