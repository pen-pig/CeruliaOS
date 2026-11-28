"""Composite report-mode bitmap keyboard and numbered 16-bit mouse."""
import argparse
from pathlib import Path
import struct
from test_boot_compat import ROOT, boot, ovmf_x64_fd, wait_for
from test_usb_composite import Combo


class Bitmap(Combo):
    def __init__(self):
        super().__init__(False)
        self.reports[1]=bytes.fromhex(
            "05 01 09 06 a1 01 85 07 05 07 19 e0 29 e7 15 00 25 01 "
            "75 01 95 08 81 02 19 00 29 7f 95 80 81 02 c0")
        self.reports[2]=bytes.fromhex(
            "05 01 09 02 a1 01 85 09 09 01 a1 00 "
            "05 09 19 01 29 03 15 00 25 01 75 01 95 03 81 02 75 05 95 01 81 01 "
            "05 01 09 30 09 31 16 01 80 26 ff 7f 75 10 95 02 81 06 "
            "09 38 15 81 25 7f 75 08 95 01 81 06 c0 c0")
        last=-1
        offset=9
        while offset<len(self.config):
            size,kind=self.config[offset:offset+2]
            if kind==4:
                last=self.config[offset+2]
                if last in (1,2):self.config[offset+6:offset+8]=bytes(2)
            if last in (1,2) and kind==0x21:
                struct.pack_into("<H",self.config,offset+7,len(self.reports[last]))
            if last in (1,2) and kind==5:
                struct.pack_into("<H",self.config,offset+4,18 if last==1 else 7)
            offset+=size

    def announce(self):
        tail=lambda vals:bytes(vals).ljust(32,b"\0")
        self.send(4,struct.pack("<I",3)+tail([0,1,2])+tail([3,3,3])+tail([])+tail([]))
        types=[255]*32; intervals=[0]*32; interfaces=[0]*32; mps=[0]*32
        types[0]=types[16]=0;mps[0]=mps[16]=64
        for i,sz in enumerate((1,18,7)):
            ep=17+i
            types[ep]=3;intervals[ep]=8;interfaces[ep]=i;mps[ep]=sz
        self.send(5,bytes(types+intervals+interfaces)+struct.pack("<32H",*mps))
        self.send(1,struct.pack("<BBBBHHH",1,0,0,0,0x627,0x1234,0x100))
        self.ready.set()

    def keys(self,*codes,mod=0,report_id=7):
        bitmap=bytearray(16)
        for k in codes:bitmap[k//8]|=1<<(k%8)
        self.report(0x82,bytes([report_id,mod])+bitmap)


def run(image,baseline):
    peer=Bitmap()
    def guest(q,s,_):
        if baseline:
            assert "USB-KBD: bitmap kbd" not in s.snapshot()
            assert not peer.ep_started[0x82].is_set()
            print("BASELINE: bitmap interface wasn't claimed")
            return
        start=s.size()
        for code,prefix in ((25,"v"),(8,"ve"),(21,"ver"),(40,None)):
            peer.keys(code)
            wait_for(lambda:s.tail_from(start),"] > "+prefix if prefix else "Cerulia Kernel version")
            peer.keys()
        start=s.size()
        peer.keys(*range(4,11))
        wait_for(lambda:s.tail_from(start),"] > abcdefg")
        peer.keys()
        peer.keys(4,mod=2)
        wait_for(lambda:s.tail_from(start),"] > abcdefgA")
        print("PASS NKRO report ID, key release, Shift, and seven simultaneous keys")
        start=s.size()
        peer.report(0x83,struct.pack("<BBhhb",9,1,300,-200,1))
        wait_for(lambda:s.tail_from(start),"USB-MOU: move dx=300 dy=-200 btn=1")
        print("PASS numbered 16-bit mouse report delivers signed motion and button state")
    try:
        boot("bitmap HID keyboard",["-machine","q35","-m","256",
             "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),
             "-chardev",f"socket,id=wire,host=127.0.0.1,port={peer.port}",
             "-device","usb-redir,bus=usb.0,port=3,chardev=wire"],
             [] if baseline else ["USB-KBD: bitmap kbd addr="],
             image=image,usb_boot=True,after=guest)
        if peer.error:raise peer.error
    finally:peer.close()

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image",type=Path,default=ROOT/"usb-image.bin")
    p.add_argument("--baseline",action="store_true")
    a=p.parse_args()
    run(a.image,a.baseline)
