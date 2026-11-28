"""UEFI USB-only boot. Verify real raster pixels, image links and CSP."""
import argparse
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_boot_compat import ROOT, boot, ovmf_x64_fd
from test_desktop_accounts import click_at, key, screenshot, state

FIX = Path(__file__).parent / "fixtures" / "browser"


def check(image, live=False, baseline=False):
    seen = []
    class Peer(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            mime, body = "text/html", b"followed"
            if self.path == "/test":
                body = b'<a href="/clicked"><img alt="Photo" src="/p.png"></a><img src="/j.jpg"><img src="/p.png">'
            elif self.path == "/many":
                body=b"".join(f'<img src="/p.png?{i}">'.encode() for i in range(10))
            elif self.path.partition("?")[0] in ("/p.png", "/j.jpg"):
                jpeg = self.path.endswith(".jpg")
                mime = "image/jpeg" if jpeg else "image/png"
                body = (FIX / ("halves.jpg" if jpeg else "halves.png")).read_bytes()
            elif self.path == "/blocked":
                body = b'<img src="/never"><p>policy test</p>'
            self.send_response(200)
            if self.path == "/blocked": self.send_header("Content-Security-Policy", "img-src 'none'")
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    out = ROOT / "temp" / "browser-raster"
    out.mkdir(parents=True,exist_ok=True)
    with ThreadingHTTPServer(("127.0.0.1",0), Peer) as srv:
        server_thread=threading.Thread(target=srv.serve_forever,daemon=True)
        server_thread.start()
        base = f"http://10.0.2.2:{srv.server_port}"
        def guest(q, serial, scratch):
            q.type_line("desktop")
            time.sleep(4)
            key(q,"b")
            def go(url):
                key(q,"f1")
                q.type_line(url)
            def wait(pattern, seconds=50):
                end=time.monotonic()+seconds
                while time.monotonic()<end:
                    start=serial.size(); state(q,serial)
                    rep=serial.tail_from(start)
                    if re.search(pattern,rep): return rep
                    time.sleep(.2)
                raise AssertionError(rep)
            go(base+"/test")
            wait(r'browser status=200 bytes=[1-9]\d* nodes=[1-9]')
            if baseline:
                time.sleep(2)
                assert seen==["/test"],seen
                print("BASELINE: page returned 200, neither image requested",flush=True)
                return
            rep=wait(r'images=2 ')
            assert seen.count("/p.png")==seen.count("/j.jpg")==1,seen
            x,y,w,h=map(int,re.search(r'app=8 x=(\d+) y=(\d+) w=(\d+) h=(\d+)',rep).groups())
            time.sleep(.5)
            width,_,rgb=screenshot(q,out/"native-images.ppm")
            def pixel(xx,yy): return tuple(rgb[(yy*width+xx)*3:(yy*width+xx)*3+3])
            def find_row(run):
                for row in range(y+36,y+h-10):
                    lo=(row*width+x)*3; hi=(row*width+x+w-10)*3
                    off=rgb.find(run,lo,hi)
                    if off>=0 and (off-lo)%3==0:return (off-lo)//3+x,row
                raise AssertionError("bitmap color pattern missing")
            px,py=find_row(bytes([255,0,0])*24+bytes([0,255,0])*24)
            # Last four rows of the PNG are transparent (page background).
            assert min(pixel(px+8,py+21))>=232
            find_row(bytes([0,0,255])*12)
            click_at(q,serial,px+6,py+6)
            wait(r'browser status=200 bytes=8 nodes=1 .*images=0')
            assert '/clicked' in seen,seen
            go(base+"/blocked")
            wait(r'browser status=200 bytes=36 nodes=[1-9].*images=0')
            time.sleep(1)
            assert '/never' not in seen
            print("PASS PNG split colors and transparency, JPEG color, duplicate fetch coalescing, image link and CSP",flush=True)
            go(base+"/many")
            wait(r'images=10 ',75)
            assert all(seen.count(f"/p.png?{i}")==1 for i in range(10)),seen
            print("PASS 10 distinct visible images on the same page",flush=True)
            if live:
                for name,url in [('df','https://df.qq.com/cp/a20240906main/index.html'),('luogu','https://www.luogu.com.cn/')]:
                    go(url)
                    rep=wait(r'browser status=200 bytes=\d{4,} nodes=[1-9].*images=[1-9]\d* ',180)
                    time.sleep(.6); screenshot(q,out/(name+'-native.ppm'))
                    print('PASS',url,re.search(r'browser status=.*',rep)[0],flush=True)
        try:
            boot("native Browser, emulated USB only",["-machine","q35","-cpu","max","-m","256",
                 "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),
                 "-netdev","user,id=n0","-device","e1000,netdev=n0"],
                 ["EZEFI:x64 entering i686"], image=image,usb_boot=True,after=guest)
        finally:
            srv.shutdown()
            server_thread.join()


if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--image",type=Path,default=ROOT/"usb-image.bin")
    ap.add_argument("--live",action="store_true")
    ap.add_argument("--baseline",action="store_true")
    args=ap.parse_args()
    check(args.image.resolve(),args.live,args.baseline)
