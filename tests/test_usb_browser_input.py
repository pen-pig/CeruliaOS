"""End-to-end USB keyboard + relative wheel mouse, no i8042 or serial input."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
from threading import Thread
import time
from test_boot_compat import ROOT, boot, ovmf_x64_fd, wait_for
from test_desktop_accounts import screenshot

def tap(q, *keys):
    for down in (True, False):
        ev = [{"type":"key", "data":{"down":down,"key":{"type":"qcode","data":k}}}
              for k in (keys if down else reversed(keys))]
        assert "return" in q.cmd("input-send-event", events=ev)
        time.sleep(.18)

def run(image, baseline, uhci):
    class Peer(BaseHTTPRequestHandler):
        def do_GET(self):
            body = (b'<a href="#target">USB CLICK</a>' + b"<p>USB Browser line</p>" * 100 +
                    b'<p id="target">Target</p>' + b"<p>Bottom</p>" * 50)
            self.send_response(200)
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    with ThreadingHTTPServer(("127.0.0.1",0), Peer) as peer:
        Thread(target=peer.serve_forever,daemon=True).start()
        def guest(q,s,tmp):
            def line(text):
                for c in text:
                    keys={":":("shift","semicolon"),"/":("slash",),".":("dot",)," ":("spc",)}
                    tap(q,*keys.get(c,(c,)))
                tap(q,"ret")
            start=s.size()
            tap(q,"caps_lock");tap(q,"a");tap(q,"shift","b");tap(q,"caps_lock");tap(q,"c")
            expected="aBc" if baseline else "Abc"
            wait_for(lambda:s.tail_from(start),"] > "+expected)
            print("USB Caps/Shift shell echo:",expected,flush=True)
            if baseline:return
            for remaining in ("Ab","A",""):
                start=s.size();tap(q,"backspace");wait_for(lambda:s.tail_from(start),lambda t:t.endswith("] > "+remaining))
            for i,ch in enumerate("desktop",1):
                start=s.size();tap(q,ch);wait_for(lambda:s.tail_from(start),"] > "+"desktop"[:i])
            tap(q,"ret");time.sleep(4);tap(q,"b");tap(q,"f1")
            line("http://10.0.2.2:"+str(peer.server_port)+"/")
            def report():
                # A fixed-duration HMP key tap may land entirely within one
                # slow redraw. Keep F12 down until the guest acknowledges.
                start=s.size()
                def f12(down):
                    assert "return" in q.cmd("input-send-event",events=[{"type":"key","data":{
                        "down":down,"key":{"type":"qcode","data":"f12"}}}])
                f12(True)
                try:return wait_for(lambda:s.tail_from(start),"[GW] end")
                finally:f12(False);time.sleep(.25)
            def scroll():
                r=report()
                m=re.search(r"browser status=200 .* scroll=(\d+) ",r)
                return int(m[1]) if m else -1
            for _ in range(20):
                if scroll()==0:break
            else:raise AssertionError("long page not at top")
            def wheel(button):
                for down in (True,False):
                    assert "return" in q.cmd("input-send-event", events=[{"type":"btn","data":{"button":button,"down":down}}])
                    time.sleep(.2)
            wheel("wheel-down"); offset=scroll();assert offset>0,offset
            wheel("wheel-up");assert scroll()==0
            tap(q,"pgdn");page=scroll();assert page>offset*2,(offset,page)
            tap(q,"pgup");assert scroll()==0
            x,y=map(int,re.search(r"app=8 x=(\d+) y=(\d+)",report()).groups())
            stride,_,rgb=screenshot(q,tmp/"link.ppm")
            def blue(px,py):
                r,g,b=rgb[3*(py*stride+px):3*(py*stride+px)+3]
                return b>150 and b>r+90 and b>g+50
            point=next((px,py) for py in range(y+38,y+85) for px in range(x+1,x+160) if blue(px,py))
            for _ in range(60):
                mx,my=map(int,re.search(r" mx=(\d+) my=(\d+)",report()).groups())
                dx,dy=point[0]-mx,point[1]-my
                if not dx and not dy:break
                q.hmc(f"mouse_move {max(-95,min(95,dx))} {max(-95,min(95,dy))}")
                time.sleep(.18)
            else:raise AssertionError("USB pointer never reached link")
            q.hmc("mouse_button 1");time.sleep(.5);q.hmc("mouse_button 0")
            for _ in range(12):
                if scroll()>page:break
            else:raise AssertionError("USB left click did not follow the page anchor")
            print("USB pointer moved to link and clicked through to the page anchor",flush=True)
            def edges():
                return int(re.search(r" tr=(\d+)",report())[1])
            prior=edges()
            q.hmc("mouse_button 1")
            for _ in range(6):
                if edges()==prior+1:break
            else:raise AssertionError("left button press did not reach the guest")
            assert "return" in q.cmd("device_del",id="mou")
            for _ in range(6):
                if edges()==prior+2:break
            else:raise AssertionError("mouse unplug did not release the button")
            print("USB mouse unplug released left button",flush=True)
            print(f"USB wheel down/up: {offset}/0; page down/up: {page}/0",flush=True)
        def checked_guest(q,s,tmp):
            try:guest(q,s,tmp)
            except Exception:
                print(s.snapshot()[-4500:],flush=True);raise
        try:
            bus="hid.0" if uhci else "usb.0"
            host=["-device","piix3-usb-uhci,id=hid"] if uhci else []
            boot("USB browser input via "+bus,["-machine","q35,i8042=off","-cpu","max","-m","256",
                 "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),
                 *host,"-device","usb-kbd,bus="+bus+(",port=1" if uhci else ""),
                 "-device","usb-mouse,id=mou,bus="+bus+(",port=2" if uhci else ""),
                 "-netdev","user,id=n0","-device","e1000,netdev=n0"],
                 ["Keyboard: using USB HID","USB-MOU: boot mouse"],usb_keyboard=True,
                 usb_boot=True,image=image,after=checked_guest)
        finally:peer.shutdown()

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--baseline",action="store_true")
    p.add_argument("--image",default=ROOT/"usb-image.bin")
    p.add_argument("--uhci",action="store_true")
    a=p.parse_args();run(a.image,a.baseline,a.uhci)

