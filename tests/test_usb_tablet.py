"""Check a report-mode (non-boot) USB absolute pointer through xHCI."""
import re
import time
from test_boot_compat import ROOT, boot, wait_for, type_on_usb, ovmf_x64_fd

def guest(q,s,tmp):
    start=s.size()
    type_on_usb(q,s.snapshot,"desktop","] > ",lambda _: "desktop" in s.tail_from(start))
    time.sleep(4)
    def state():
        start=s.size();q.hmc("sendkey f12 300")
        return wait_for(lambda:s.tail_from(start),"[GW] end")
    def position(x,y):
        assert "return" in q.cmd("input-send-event",events=[
            {"type":"abs","data":{"axis":axis,"value":v}} for axis,v in (("x",x),("y",y))])
        time.sleep(.3)
        return tuple(map(int,re.search(r" mx=(\d+) my=(\d+)",state()).groups()))
    first=position(8192,8192);second=position(24576,24576)
    assert all(a>0 and abs(b-3*a)<4 for a,b in zip(first,second)),(first,second)
    edges=lambda:int(re.search(r" tr=(\d+)",state())[1])
    n=edges()
    for down in (True,False):
        assert "return" in q.cmd("input-send-event",events=[{"type":"btn","data":{"button":"left","down":down}}])
        time.sleep(.3)
    assert edges()==n+2
    print("PASS non-boot USB tablet absolute coordinates and button edges:",first,second,flush=True)

if __name__=="__main__":
    boot("USB HID tablet",["-machine","q35,i8042=off","-cpu","max","-m","256",
         "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),
         "-device","usb-kbd,bus=usb.0","-device","usb-tablet,bus=usb.0"],
         ["Keyboard: using USB HID","mode=absolute"],image=ROOT/"usb-image.bin",usb_boot=True,usb_keyboard=True,after=guest)
