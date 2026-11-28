"""Verify styling of a real document rendered after booting only usb-image.bin."""
import argparse
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_boot_compat import ROOT, boot, ovmf_x64_fd
from test_desktop_accounts import click_at, key, screenshot, state

PAGE = b"""<!doctype html><html><head>
<link rel="stylesheet" href="/s/first.css?v=1&amp;lang=zh">
<link rel='stylesheet' href='s/last.css' media='screen and (min-width: 600px)'>
<style> html { font-size: 100%; } .letter {color:#551133;} </style></head>
<body><div class="shell"><div style="color:#c509f6" class="vanish">MUST NOT APPEAR</div>
<p><a class="letter" href="/opened" style="color:red">COLOR</a></p>
<p class="cascade">ORDER</p></div>
<div class="inherit"><span>INHERIT</span></div></body></html>"""
FIRST = b"""/* source-file comment */ body {background:#19324b}
.shell .letter {color:#405070!important;}
.shell>.vanish {display:none}
.cascade {color:#cd0104}
.inherit { color:rgb(30, 170, 72) }
"""
LAST = b"""@media print { .letter { display:none } }
@media screen and (min-width: 680px) {
   .shell .letter {font-size:2rem;text-align:center;color:#fdcc28!important}
}
.cascade { color:#e8a1b7 }
"""
BLOCKED = (b'<meta http-equiv="Content-Security-Policy" content="default-src none; style-src \'none\'">'
           b'<link rel=stylesheet href=/denied.css><p style="color:red">Unstyled</p>')


def run(image, live, baseline):
    seen = []
    class Peer(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body, mime = {
                "/page": (PAGE, "text/html"), "/s/first.css?v=1&lang=zh": (FIRST, "text/css"),
                "/s/last.css": (LAST, "text/css; charset=utf-8"),
                "/blocked": (BLOCKED,"text/html"),"/opened": (b"Opened","text/html"),
            }.get(self.path,(b"", "text/css"))
            self.send_response(200)
            self.send_header("Content-Type",mime)
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    out=ROOT/"temp"/"browser-css"
    out.mkdir(exist_ok=True,parents=True)
    with ThreadingHTTPServer(("127.0.0.1",0),Peer) as server:
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        base=f"http://10.0.2.2:{server.server_port}"
        def guest(q,serial,scratch):
            q.type_line("desktop")
            time.sleep(4)
            key(q,"b")
            def go(url):
                key(q,"f1")
                q.type_line(url)
            def wait(pat,seconds=75):
                until=time.monotonic()+seconds
                while time.monotonic()<until:
                    start=serial.size();state(q,serial);report=serial.tail_from(start)
                    if re.search(pat,report):return report
                    time.sleep(.3)
                raise AssertionError(report)
            go(base+"/page")
            if baseline:
                wait(f"browser status=200 bytes={len(PAGE)} ")
                time.sleep(1);assert seen==["/page"],seen
                print("BEFORE: document loaded, no stylesheets requested",flush=True)
                return
            rep=wait(r" css=2 ")
            assert seen[:3]==["/page","/s/first.css?v=1&lang=zh","/s/last.css"],seen
            x,y,w,h=map(int,re.search(r"app=8 x=(\d+) y=(\d+) w=(\d+) h=(\d+)",rep).groups())
            time.sleep(.5)
            width,_,data=screenshot(q,out/"styled.ppm")
            def coords(rgb):
                return [(xx,yy) for yy in range(y+40,min(y+h-10,y+255)) for xx in range(x+1,x+w-8)
                        if data[(yy*width+xx)*3:(yy*width+xx+1)*3]==rgb]
            def lit(hexrgb):return coords(bytes.fromhex(hexrgb))
            primary=lit("fdcc28")
            assert len(lit("19324b"))>10000 and lit("e8a1b7") and lit("1eaa48")
            assert not lit("c509f6") and not lit("cd0104") and not lit("405070")
            assert primary and max(v for _,v in primary)-min(v for _,v in primary)>=16
            assert abs(sum(v for v,_ in primary)/len(primary)-(x+w/2))<50
            click_at(q,serial,*primary[len(primary)//2])
            wait("browser status=200 bytes=6 ")
            assert seen[-1]=="/opened",seen
            go(base+"/blocked")
            wait(f"browser status=200 bytes={len(BLOCKED)} .* css=0 ")
            time.sleep(.4)
            assert "/denied.css" not in seen,seen
            print("PASS external and inline cascade, selectors, source order, !important, screen media, inheritance, hidden content, hit test, CSP",flush=True)
            if live:
                for tag,url in [("df","https://df.qq.com/cp/a20240906main/index.html"),
                                ("luogu","https://www.luogu.com.cn/")]:
                    go(url)
                    report=wait(r"browser status=200 .* css=[2-9]\d* ",140)
                    xx,yy,ww=map(int,re.search(r"app=8 x=(\d+) y=(\d+) w=(\d+)",report).groups())
                    for _ in range(20):
                        time.sleep(.48)
                        stride,_,rgb=screenshot(q,out/(tag+"-css.ppm"))
                        # Avoid capturing the refresh after wallpaper but before
                        # the browser window has been painted.
                        off=(stride*(yy+24)+xx+ww-60)*3
                        if min(rgb[off:off+3])>235:break
                    else:raise AssertionError("browser address field not painted")
                    print("PASS real stylesheet fetch",url,re.search(r"browser status=.*",report)[0],flush=True)
        try:
            boot("CSS over native HTTPS or HTTP",["-machine","q35","-cpu","max","-m","256",
                 "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),
                 "-netdev","user,id=n0","-device","e1000,netdev=n0"],
                 ["EZEFI:x64 entering i686"],image=image,usb_boot=True,after=guest)
        finally:
            server.shutdown()
            worker.join()


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--live",action="store_true")
    p.add_argument("--baseline",action="store_true")
    p.add_argument("--image",type=Path,default=ROOT/"usb-image.bin")
    a=p.parse_args()
    run(a.image.resolve(),a.live,a.baseline)
