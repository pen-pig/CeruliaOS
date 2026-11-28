"""Hold, release, and detach a real emulated USB boot keyboard."""
import argparse
from pathlib import Path
import time

from test_boot_compat import ROOT, boot, ovmf_x64_fd, wait_for

def event(q, key, down):
    assert "return" in q.cmd("input-send-event", events=[
        {"type":"key","data":{"down":down,"key":{"type":"qcode","data":key}}}])

def exercise(image, uhci, baseline):
    def guest(q,s,tmp):
        def tap(key):
            event(q,key,True);time.sleep(.2)
            event(q,key,False);time.sleep(.2)
        def expect(start, text):
            wait_for(lambda:s.tail_from(start),"] > "+text)
        start=s.size()
        event(q,"x",True)
        try:
            expect(start,"x")
            if baseline:
                time.sleep(2)
                assert "] > xx" not in s.tail_from(start)
                print("BASELINE held x typed once",flush=True)
                return
            expect(start,"xxxx")
        finally:event(q,"x",False)
        time.sleep(.5)
        stop=s.size()
        time.sleep(1)
        assert stop==s.size(),"key still repeating after release"
        held_line=s.tail_from(start).rsplit("] > ",1)[1]
        assert len(held_line)>=4 and set(held_line)=={"x"},repr(held_line)

        tap("num_lock");start=s.size();tap("kp_1");tap("kp_add");tap("kp_2")
        expect(start,held_line+"1+2")
        # Toggle back to navigation: keypad 4 moves the cursor left.
        tap("num_lock");tap("kp_4")
        start=s.size();tap("a");expect(start,held_line+"1+a2")
        print("PASS USB held character, release, keypad digits/operators and Num Lock navigation",flush=True)

        event(q,"y",True)
        start=s.size()
        wait_for(lambda:s.tail_from(start),lambda t:"ayy2" in t)
        assert "return" in q.cmd("device_del",id="kbd")
        time.sleep(1)
        stop=s.size();time.sleep(1)
        assert stop==s.size(),"key still repeating after unplug"
        print("PASS unplugging held USB keyboard stops repeat",flush=True)

    host=["-device","piix3-usb-uhci,id=hid"] if uhci else []
    bus="hid.0,port=1" if uhci else "usb.0"
    boot("USB keyboard hold / "+("UHCI" if uhci else "xHCI"),
         ["-machine","q35,i8042=off","-cpu","max","-m","256",
          "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),
          *host,"-device","usb-kbd,id=kbd,bus="+bus],
         ["Keyboard: using USB HID"],image=image,usb_boot=True,usb_keyboard=True,after=guest)

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--image",type=Path,default=ROOT/"usb-image.bin")
    p.add_argument("--uhci",action="store_true")
    p.add_argument("--baseline",action="store_true")
    args=p.parse_args()
    exercise(args.image,args.uhci,args.baseline)
