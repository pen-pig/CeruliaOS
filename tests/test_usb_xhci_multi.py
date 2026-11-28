"""Two xHCI controllers, no PS/2; boot keyboard is only on the second."""
import argparse
from pathlib import Path
from test_boot_compat import ROOT, boot, ovmf_x64_fd, wait_for
from test_usb_ehci_hid import check_keys, mouse


def run(image, baseline=False, bios=False, four=False):
    names="abcd" if four else "ab"
    last=names[-1]+".0"
    hw=["-machine","q35,i8042=off","-cpu","max","-m","256"]
    for index,name in enumerate(names):
        model="qemu-xhci" if index%2==0 else "nec-usb-xhci"
        hw+=["-device",f"{model},id={name},addr={index+5:02x}.0"]
    hw+=["-device",f"usb-kbd,bus={last},port=1,id=kbd","-device","usb-mouse,bus=a.0,port=1"]
    if not bios:
        hw+=["-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd()]

    def guest(q,s,_):
        ready=s.snapshot().count("xHCI: controller ready")
        if baseline:
            assert ready==1,ready
            print("BASELINE: only one xHCI ready and the last keyboard could not log in")
            return
        assert ready==len(names),ready
        mouse(q,s)
        start=s.size()
        assert "return" in q.cmd("device_del",id="kbd")
        wait_for(lambda:s.tail_from(start),"USB-HID: detached xHCI root port")
        assert "return" in q.cmd("device_add",driver="usb-kbd",bus=last,port="1",id="kbd2")
        wait_for(lambda:s.tail_from(start),"USB-HID: attached xHCI root port")
        check_keys(q,s)
        mouse(q,s)
        print(f"PASS {len(names)} xHCI instances, independent rings and keyboard replug on the last controller")

    expected=["COM1 rescue input active"] if baseline else ["Keyboard: using USB HID"]
    if not bios:expected.append("xHCI: using PAE window for BAR above 4 GiB")
    boot(f"{len(names)} xHCI controllers ("+("BIOS" if bios else "x64 UEFI")+")",hw,
         expected,image=image,rescue=baseline,usb_keyboard=not baseline,after=guest)


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image",type=Path,default=ROOT/"usb-image.bin")
    p.add_argument("--baseline",action="store_true")
    p.add_argument("--bios",action="store_true")
    p.add_argument("--four",action="store_true")
    a=p.parse_args()
    run(a.image,a.baseline,a.bios,a.four)
