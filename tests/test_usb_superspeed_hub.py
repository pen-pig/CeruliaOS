"""Enumerate a synthetic SuperSpeed hub on stock QEMU; no child transport."""
import argparse
from pathlib import Path
import struct
from test_boot_compat import ROOT, boot, ovmf_x64_fd, wait_for
from test_usb_ehci_hub import Hub

class SuperHub(Hub):
    def __init__(self):
        super().__init__()
        self.dev=bytes.fromhex("12 01 00 03 09 00 03 09 27 06 34 12 00 01 00 00 00 01")
        self.config=bytes.fromhex("09 02 1f 00 01 01 00 e0 00 09 04 00 00 01 09 00 00 00 "
                                  "07 05 81 03 01 00 0c 06 30 00 00 01 00")

    def announce(self):
        tail=lambda vals:bytes(vals).ljust(32,b"\0")
        self.send(4,struct.pack("<I",1)+tail([0])+tail([9])+tail([0])+tail([0]))
        kinds=[255]*32
        mps=[0]*32
        kinds[0]=kinds[16]=0
        mps[0]=mps[16]=512
        kinds[17],mps[17]=3,1
        self.send(5,bytes(kinds)+bytes(17)+bytes([12])+bytes(14)+bytes(32)+struct.pack("<32H",*mps))
        self.send(1,struct.pack("<BBBBHHH",3,9,0,3,0x627,0x1234,0x100))
        self.ready.set()

    def control(self,req,typ,value,index,nbytes,data):
        if (typ,req,value)==(0xa0,6,0x2a00):
            reply=bytes.fromhex("0c 2a 02 01 00 01 00 00 00 00 00 00")[:nbytes]
        elif (typ,req,value)==(0x80,6,0x0f00):
            reply=bytes.fromhex("05 0f 0f 00 01 0a 10 03 00 0e 00 01 0a ff 07")[:nbytes]
        elif (typ,req)==(0x20,12):
            self.requests.append((typ,req,value,index))
            return 0,0,b""
        elif (typ,req)==(0xa3,0):
            self.requests.append((typ,req,value,index))
            reply=struct.pack("<HH",0x0200,0)
        else:return super().control(req,typ,value,index,nbytes,data)
        return 0,len(reply),reply

def run(image):
    peer=SuperHub()
    def guest(q,s,_):
        assert "USB-HID: SuperSpeed hub ready" in s.snapshot()
        assert (0x20,12,0,0) in peer.requests
        assert (0xa3,0,0,2) in peer.requests
        print("PASS SuperSpeed descriptor, root hub depth and port status (synthetic hub)")
        start=s.size()
        peer.send(2)
        wait_for(lambda:s.tail_from(start),"USB-HID: detached xHCI root port")
        start=s.size()
        peer.announce()
        wait_for(lambda:s.tail_from(start),"USB-HID: SuperSpeed hub ready")
        print("PASS SuperSpeed Hub root replug")
    try:
        boot("SuperSpeed USB Hub",["-machine","q35","-m","256",
             "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),
             "-chardev",f"socket,id=wire,host=127.0.0.1,port={peer.port}",
             "-device","usb-redir,bus=usb.0,port=3,chardev=wire"],
             ["USB-HID: SuperSpeed hub ready"],image=image,usb_boot=True,after=guest)
        if peer.error:raise peer.error
    finally:peer.close()

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image",type=Path,default=ROOT/"usb-image.bin")
    run(p.parse_args().image)
