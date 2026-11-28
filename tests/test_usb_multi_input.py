"""Independently drive two composite USB receivers over local usb-redir."""
import argparse
from pathlib import Path
import time
from test_boot_compat import ROOT, boot, ovmf_x64_fd, wait_for
from test_usb_composite import Combo


def keys(peer,code=0,mod=0):
    peer.report(0x82,bytes((mod,0,code,0,0,0,0,0)))


def motion(peer,s,dx,dy,local,merged):
    start=s.size()
    for _ in range(8):
        peer.report(0x83,bytes((local,dx&255,dy&255,0)))
        time.sleep(.48)
        if f"USB-MOU: move dx={dx} dy={dy} btn={merged} " in s.tail_from(start):
            return
    raise AssertionError(s.tail_from(start))


def run(image, split, baseline):
    a,b=Combo(False),Combo(False)
    extra=["-device","nec-usb-xhci,id=second"] if split else []
    second_bus="second.0,port=1" if split else "usb.0,port=4"

    def guest(q,s,_):
        if baseline:
            assert s.snapshot().count("USB-KBD: boot kbd")==1
            assert s.snapshot().count("USB-MOU: boot mouse")==1
            print("BASELINE: only one of the two composite receivers bound for input")
            return
        assert s.snapshot().count("USB-KBD: boot kbd")==2
        assert s.snapshot().count("USB-MOU: boot mouse")==2
        start=s.size()
        for peer,code,prefix in ((a,25,"v"),(b,8,"ve"),(a,21,"ver"),(b,40,None)):
            keys(peer,code)
            wait_for(lambda:s.tail_from(start),"] > "+prefix if prefix else "Cerulia Kernel version")
            keys(peer)
        print("PASS both keyboards contribute to a single shell command")
        for bits in (2,0):
            starts=(len(a.leds),len(b.leds))
            keys(a,0x39)
            wait_for(lambda:str((a.leds[starts[0]:],b.leds[starts[1]:])),
                     lambda _:bits in a.leds[starts[0]:] and bits in b.leds[starts[1]:])
            keys(a)
            time.sleep(.1)
        print("PASS Caps Lock setting sent to both keyboards")
        motion(a,s,17,-9,1,1)
        motion(b,s,29,-13,0,1)
        motion(b,s,31,-11,2,3)
        motion(b,s,30,-12,1,1) # both receivers now hold left
        keys(a,mod=2)
        time.sleep(.15)
        start=s.size()
        keys(b,4)
        wait_for(lambda:s.tail_from(start),"] > A")
        keys(b)
        start=s.size()
        a.send(2)
        wait_for(lambda:s.tail_from(start),"USB-HID: detached xHCI root port")
        motion(b,s,27,-7,1,1)
        motion(b,s,19,7,0,0)
        keys(b,4)
        wait_for(lambda:s.tail_from(start),"] > Aa")
        keys(b)
        print("PASS two pointers merge buttons; unplug releases only the missing device")
        print("PASS cross-keyboard Shift and modifier release on unplug")
        for event in a.ep_started.values():event.clear()
        start=s.size()
        a.announce()
        wait_for(lambda:s.tail_from(start),"USB-HID: attached xHCI root port")
        keys(a,5)
        wait_for(lambda:s.tail_from(start),"] > Aab")
        keys(a)
        motion(a,s,21,6,0,0)
        print("PASS replugged receiver's keyboard and mouse bind while another pair stays active")

    try:
        boot("two USB receivers",["-machine","q35","-m","256",
             "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),*extra,
             "-chardev",f"socket,id=wirea,host=127.0.0.1,port={a.port}",
             "-chardev",f"socket,id=wireb,host=127.0.0.1,port={b.port}",
             "-device","usb-redir,bus=usb.0,port=3,chardev=wirea",
             "-device","usb-redir,bus="+second_bus+",chardev=wireb"],
             image=image,usb_boot=True,after=guest)
        for peer in (a,b):
            if peer.error:raise peer.error
    finally:
        a.close()
        b.close()


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image",type=Path,default=ROOT/"usb-image.bin")
    p.add_argument("--split",action="store_true")
    p.add_argument("--baseline",action="store_true")
    a=p.parse_args()
    run(a.image,a.split,a.baseline)
