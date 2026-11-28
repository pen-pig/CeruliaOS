"""Composite keyboard/pointer fixture over QEMU usb-redir; loopback only."""
import argparse
import socket
import struct
import threading
import time
from pathlib import Path
from test_boot_compat import ROOT, boot, ovmf_x64_fd, wait_for

class Combo:
    def __init__(self,long_config,low=False,high=False):
        self.low=low
        self.speed=2 if high else 0 if low else 1
        self.ep_started={n: threading.Event() for n in (0x81,0x82,0x83)}
        self.ready=threading.Event()
        self.header="<III"
        self.send_lock=threading.Lock()
        self.error=None
        self.leds=[]
        self.proto={}
        self.refuse_mode_switch=False
        self.conf=0
        self.kbd=bytes.fromhex(
            "05 01 09 06 a1 01 05 07 19 e0 29 e7 15 00 25 01 75 01 95 08 81 02"
            " 75 08 95 01 81 01 19 00 29 65 15 00 25 65 75 08 95 06 81 00 c0")
        self.mouse=bytes.fromhex(
            "05 01 09 02 a1 01 05 09 19 01 29 03 15 00 25 01 75 01 95 03 81 02"
            " 75 05 95 01 81 01 05 01 09 30 09 31 09 38 15 81 25 7f 75 08 95 03 81 06 c0")
        self.consumer=bytes.fromhex("05 0c 09 01 a1 01 09 ea 15 00 25 01 75 01 95 01 81 02 c0")
        self.reports=[self.consumer,self.kbd,self.mouse]
        conf=bytearray()
        for i,(report,sub,proto,mps) in enumerate(zip(self.reports,(0,1,1),(0,1,2),(1,8,4))):
            conf+=bytes((9,4,i,0,1,3,sub,proto,0))
            conf+=struct.pack("<BBHBBBH",9,0x21,0x111,0,1,0x22,len(report))
            if i==0 and long_config:
                conf+=bytes([124,0x24])+bytes(122)
                conf+=bytes([124,0x24])+bytes(122)
            conf+=bytes((7,5,0x81+i,3,mps,0,8))
        self.config=bytearray(struct.pack("<BBHBBBBB",9,2,len(conf)+9,3,1,0,0x80,20))+conf
        self.mouse_ep=0x83
        self.dev=bytearray.fromhex("12 01 00 02 00 00 00 40 27 06 34 12 00 01 00 00 00 01")
        if low:self.dev[7]=8
        self.listener=socket.socket()
        self.listener.bind(("127.0.0.1",0))
        self.listener.listen(1)
        self.port=self.listener.getsockname()[1]
        self.sock=None
        self.thread=threading.Thread(target=self.server,daemon=True)
        self.thread.start()

    def close(self):
        self.listener.close()
        if self.sock:
            try:self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            self.sock.close()
        self.thread.join(1)

    def send(self,kind,payload=b"",ident=0):
        with self.send_lock:
            self.sock.sendall(struct.pack(self.header,kind,len(payload),ident)+payload)

    def move_endpoint(self):
        at=len(self.config)-5
        assert self.config[at]==0x83
        self.config[at]=self.mouse_ep=0x84
        self.refuse_mode_switch=True
        self.ep_started={n:threading.Event() for n in (0x81,0x82,0x84)}

    def announce(self):
        tail=lambda vals:bytes(vals).ljust(32,b"\0")
        self.send(4,struct.pack("<I",3)+tail([0,1,2])+tail([3,3,3])+tail([0,1,1])+tail([0,1,2]))
        types=[255]*32; intervals=[0]*32; interfaces=[0]*32; mps=[0]*32
        types[0]=types[16]=0;mps[0]=mps[16]=8 if self.low else 64
        for i,sz in enumerate((1,8,4)):
            ep=16+(self.mouse_ep&15) if i==2 else 17+i
            types[ep]=3;intervals[ep]=8;interfaces[ep]=i;mps[ep]=sz
        self.send(5,bytes(types+intervals+interfaces)+struct.pack("<32H",*mps))
        self.send(1,struct.pack("<BBBBHHH",self.speed,0,0,0,0x627,0x1234,0x100))
        self.ready.set()

    def control(self,req,typ,value,index,nbytes,data):
        status=0;reply=b"";actual=nbytes
        if typ in (0x80,0x81) and req==6:
            if value==0x100:reply=self.dev
            elif value==0x200:reply=self.config
            elif value==0x2200 and index<3:reply=self.reports[index]
            else:status=4
            reply=reply[:nbytes];actual=len(reply)
        elif typ==0x21 and req==0xb:
            if self.refuse_mode_switch:status=4
            else:self.proto[index]=value
        elif typ==0xa1 and req==3:
            reply=bytes((self.proto.get(index,0 if index==1 else 1),));actual=1
        elif typ==0x21 and req==9 and index==1 and data:
            self.leds.append(data[0])
        else:
            status=4;actual=0
        return status,actual,reply

    def server(self):
        try:
            self.sock,_=self.listener.accept()
            with self.sock.makefile("rb") as f:
                # xHCI requires 64-bit IDs, endpoint sizes and bulk length capabilities.
                self.send(0,b"Cerulia synthetic HID receiver".ljust(64,b"\0")+struct.pack("<I",114))
                while True:
                    head=f.read(struct.calcsize(self.header))
                    if not head:return
                    kind,n,ident=struct.unpack(self.header,head)
                    assert n<=4096
                    data=f.read(n)
                    if kind==0:
                        assert struct.unpack_from("<I",data,64)[0]&32
                        self.header="<IIQ"
                        self.announce()
                    elif kind==3:
                        self.conf=0
                        self.proto.clear()
                    elif kind in (6,7):
                        if kind==6:self.conf=data[0]
                        self.send(8,bytes((0,self.conf)),ident)
                    elif kind==15:
                        ep=data[0]
                        self.send(17,bytes((0,ep)),ident)
                        self.ep_started[ep].set()
                    elif kind==100:
                        ep,req,typ,_,value,index,nbytes=struct.unpack_from("<BBBBHHH",data)
                        status,actual,reply=self.control(req,typ,value,index,nbytes,data[10:])
                        self.send(100,struct.pack("<BBBBHHH",ep,req,typ,status,value,index,actual)+reply,ident)
                    elif kind not in (16,21,22,23,24):
                        raise AssertionError((kind,n,data))
        except (ConnectionResetError,BrokenPipeError):
            return
        except Exception as e:
            self.error=e

    def report(self,ep,data):
        assert self.ep_started[ep].wait(4),(ep,self.error)
        self.send(103,struct.pack("<BBH",ep,0,len(data))+data)

def run(image,uhci,baseline,ohci=False,ehci=False):
    # A longer configuration exercises receivers whose usable HID comes after
    # vendor metadata beyond byte 255. UHCI uses the compact variant here.
    peer=Combo(not uhci and not ohci and not baseline,low=ohci,high=ehci)
    def guest(q,s,tmp):
        assert peer.ready.wait(1),peer.error
        assert peer.ep_started[0x82].wait(5),s.snapshot()[-4000:]
        if baseline:
            assert not peer.ep_started[0x83].is_set()
            print("BASELINE composite receiver attached as keyboard only",flush=True)
            return
        start=s.size()
        # Queue reports on two distinct IN endpoints of one device address.
        peer.report(0x83,bytes((0,17,247,0)))
        for usage,prefix in ((25,"v"),(8,"ve"),(21,"ver"),(40,None)):
            peer.report(0x82,bytes((0,0,usage,0,0,0,0,0)))
            if prefix:
                wait_for(lambda:s.tail_from(start),"] > "+prefix)
            else:
                wait_for(lambda:s.tail_from(start),"Cerulia Kernel version")
            peer.report(0x82,bytes(8))
            time.sleep(.15)
        wait_for(lambda:s.tail_from(start),"USB-MOU: move dx=17 dy=-9")
        peer.report(0x82,bytes((0,0,0x39,0,0,0,0,0)))
        wait_for(lambda:str(peer.leds),lambda _:2 in peer.leds)
        assert peer.proto.get(1)==0 and peer.proto.get(2)==1
        assert not peer.ep_started[0x81].is_set()
        print("PASS same USB address: shell keyboard, mouse motion and Caps LED; ignored consumer control",flush=True)
        # Both periodic endpoint queues must be retired before the receiver
        # returns. Move the mouse to a different IN endpoint on re-attach.
        start=s.size()
        peer.send(2)
        wait_for(lambda:s.tail_from(start),"USB-HID: detached")
        peer.move_endpoint()
        peer.announce()
        wait_for(lambda:s.tail_from(start),"USB-HID: attached")
        start=s.size()
        peer.report(0x84,bytes((0,17,247,0)))
        wait_for(lambda:s.tail_from(start),"USB-MOU: move dx=17 dy=-9")
        peer.report(0x82,bytes((0,0,25,0,0,0,0,0)))
        wait_for(lambda:s.tail_from(start),"] > v")
        peer.report(0x82,bytes(8))
        print("PASS composite receiver reattached with a different mouse endpoint",flush=True)

    host=["-device","piix3-usb-uhci,id=hid"] if uhci else []
    if ohci:host=["-device","pci-ohci,id=hid"]
    if ehci:host=["-device","usb-ehci,id=hid"]
    bus="hid.0,port=1" if uhci or ohci or ehci else "usb.0,port=3"
    try:
        boot("composite HID receiver",["-machine","q35","-cpu","max","-m","256",
             "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),
             *host,"-chardev",f"socket,id=hidwire,host=127.0.0.1,port={peer.port}",
             "-device","usb-redir,bus="+bus+",chardev=hidwire,debug=1"],
             ["USB-KBD: boot kbd"],image=image,usb_boot=True,after=guest)
        if peer.error:raise peer.error
    finally:peer.close()

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--image",type=Path,default=ROOT/"usb-image.bin")
    p.add_argument("--uhci",action="store_true")
    p.add_argument("--ohci",action="store_true")
    p.add_argument("--ehci",action="store_true")
    p.add_argument("--baseline",action="store_true");a=p.parse_args()
    run(a.image,a.uhci,a.baseline,a.ohci,a.ehci)
