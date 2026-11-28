"""Cross-firmware USB input; no physical storage or device is touched."""
import time
from test_boot_compat import ROOT, boot, ovmf_fd

def pointer(q,s,tmp):
    start=s.size()
    # Real movement, not just an enumeration marker.
    for _ in range(7):
        result=q.cmd("input-send-event",events=[
            {"type":"rel","data":{"axis":"x","value":19}},
            {"type":"rel","data":{"axis":"y","value":-11}}])
        assert "return" in result,result
        time.sleep(.43)
        if "USB-MOU: move dx=19 dy=-11" in s.tail_from(start):break
    else:raise AssertionError(s.tail_from(start))

def run():
    image=ROOT/"usb-image.bin"
    for controller in ("piix3-usb-uhci","pci-ohci","nec-usb-xhci"):
        boot("Legacy BIOS "+controller,["-machine","pc,i8042=off","-m","128",
             "-device",controller+",id=hid","-device","usb-kbd,bus=hid.0,port=1",
             "-device","usb-mouse,bus=hid.0,port=2"],
             ["stage2: kernel image read by BIOS","Keyboard: using USB HID"],
             usb_keyboard=True,image=image,after=pointer)
    boot("IA32 UEFI USB-only input",["-machine","pc,i8042=off","-m","256",
         "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_fd(),
         "-device","usb-kbd,bus=usb.0","-device","usb-mouse,bus=usb.0"],
         ["EZEFI:ebs ok","Keyboard: using USB HID"],image=image,usb_boot=True,
         usb_keyboard=True,after=pointer)

if __name__=="__main__":run()
