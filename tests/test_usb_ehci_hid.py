"""Native EHCI high-speed keyboard/mouse, with no i8042 or companion."""
import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from test_boot_compat import ROOT, boot, ovmf_x64_fd, type_on_usb, wait_for
from tools.gen_diskimg import gen_fat

def plug(q, name="kbd"):
    reply=q.cmd("device_add",driver="usb-kbd",usb_version=2,bus="fast.0",port="1",id=name)
    assert "return" in reply,reply

def check_keys(q,s):
    start=s.size()
    type_on_usb(q,s.snapshot,"ver","] > ",lambda _:"Cerulia Kernel version" in s.tail_from(start))

def mouse(q,s):
    start=s.size()
    for _ in range(8):
        reply=q.cmd("input-send-event",events=[
            {"type":"rel","data":{"axis":"x","value":23}},
            {"type":"rel","data":{"axis":"y","value":-14}}])
        assert "return" in reply,reply
        time.sleep(.42)
        if "USB-MOU: move dx=23 dy=-14" in s.tail_from(start):return
    raise AssertionError(s.tail_from(start))

def run(image, baseline=False, cold=False, disk=None, bios=False):
    hw=["-machine","q35,i8042=off","-m","256","-device","usb-ehci,id=fast"]
    if not bios:
        hw+=["-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd()]
    if not cold:
        hw+=["-device","usb-kbd,usb_version=2,bus=fast.0,port=1,id=kbd",
             "-device","usb-mouse,usb_version=2,bus=fast.0,port=2,id=mouse"]
    if disk:
        hw+=["-drive",f"if=none,format=raw,snapshot=on,file={disk.as_posix()},id=data",
             "-device","usb-storage,bus=fast.0,port=3,drive=data"]
    def guest(q,s,tmp):
        if baseline:
            assert "USB-ENUM: SET_CONFIGURATION ok" in s.snapshot()
            print("BASELINE: EHCI configured devices but could not use them for input")
            return
        if not cold:
            mouse(q,s)
            if disk:
                start=s.size()
                q.type_line("setdrive 12")
                wait_for(lambda:s.tail_from(start),"] > ")
                start=s.size()
                q.type_line("cat README.TXT")
                wait_for(lambda:s.tail_from(start),"This is the FAT32 data disk.")
            start=s.size()
            assert "return" in q.cmd("device_del",id="kbd")
            wait_for(lambda:s.tail_from(start),"USB-HID: detached EHCI root port")
        start=s.size()
        plug(q,"newkbd")
        wait_for(lambda:s.tail_from(start),"USB-HID: attached EHCI root port")
        check_keys(q,s)
        if disk:
            start=s.size()
            q.type_line("cat README.TXT")
            wait_for(lambda:s.tail_from(start),"This is the FAT32 data disk.")
            print("PASS EHCI mass storage readable before and after keyboard replug")
        print("PASS EHCI native hot-add and repeated USB input")
    expected=["COM1 rescue input active"] if baseline or cold else ["Keyboard: using USB HID"]
    if bios:expected.append("stage2: kernel image read by BIOS")
    if disk:expected.append("registered as drive 12")
    boot("native EHCI HID",hw,expected,
         image=image,usb_boot=not bios,rescue=baseline or cold,
         usb_keyboard=not (baseline or cold),after=guest)

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image",type=Path,default=ROOT/"usb-image.bin")
    p.add_argument("--baseline",action="store_true")
    p.add_argument("--cold",action="store_true")
    p.add_argument("--storage",action="store_true")
    p.add_argument("--bios",action="store_true")
    a=p.parse_args()
    if a.storage:
        if a.cold or a.baseline:p.error("--storage requires the native boot keyboard")
        with TemporaryDirectory(prefix="cerulia-ehci-") as folder:
            disk=Path(folder)/"data.bin"
            gen_fat(str(disk),32)
            run(a.image,disk=disk,bios=a.bios)
    else:run(a.image,a.baseline,a.cold,bios=a.bios)
