"""Verify actual HID Output reports for independent caps/num toggles."""
import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
import struct
import time

from test_boot_compat import ROOT, boot, ovmf_x64_fd, wait_for

def read_led_reports(pcap):
    raw=pcap.read_bytes()
    if len(raw)<24:return []
    assert raw[:4]==bytes.fromhex("d4c3b2a1")
    linktype=struct.unpack_from("<I",raw,20)[0]
    assert linktype in (189,220),linktype # USB Linux and usbmon mmap
    at=24;codes=[]
    payload=64 if linktype==220 else 48
    while at+16<=len(raw):
        size=struct.unpack_from("<I",raw,at+8)[0]
        if at+16+size>len(raw):break # QEMU may still be writing this packet
        pkt=raw[at+16:at+16+size];at+=size+16
        if len(pkt)>payload and pkt[40:48]==bytes.fromhex("21 09 00 02 00 00 01 00"):
            codes.append(pkt[payload])
    return codes

def run(image,uhci,ohci=False,ehci=False):
    with TemporaryDirectory(prefix="cerulia-hid-") as path:
        pcap=Path(path)/"keyboard.pcap"
        def guest(q,s,tmp):
            for key,led in (("caps_lock",2),("num_lock",3),("caps_lock",1),("num_lock",0)):
                start=len(read_led_reports(pcap))
                for down in (True,False):
                    assert "return" in q.cmd("input-send-event",events=[
                        {"type":"key","data":{"key":{"type":"qcode","data":key},"down":down}}])
                    if down:
                        wait_for(lambda:str(read_led_reports(pcap)[start:]),str(led))
                    time.sleep(.2)
        host=["-device","piix3-usb-uhci,id=hid"] if uhci else []
        if ohci:host=["-device","pci-ohci,id=hid"]
        if ehci:host=["-device","usb-ehci,id=hid"]
        bus="hid.0,port=1" if uhci or ohci or ehci else "usb.0"
        boot("boot keyboard LED "+("EHCI" if ehci else "OHCI" if ohci else "UHCI" if uhci else "xHCI"),["-machine","q35,i8042=off",
             "-m","256","-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),
             *host,"-device","usb-kbd,bus="+bus+",pcap="+pcap.as_posix()],
             ["Keyboard: using USB HID"],image=image,usb_boot=True,usb_keyboard=True,after=guest)
        codes=read_led_reports(pcap)
        assert codes[-4:]==[2,3,1,0],codes
        print("PASS USB capture confirmed Num/Caps LED payloads:",codes,flush=True)

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--image",type=Path,default=ROOT/"usb-image.bin")
    p.add_argument("--ohci",action="store_true")
    p.add_argument("--ehci",action="store_true")
    p.add_argument("--uhci",action="store_true");a=p.parse_args();run(a.image,a.uhci,a.ohci,a.ehci)
