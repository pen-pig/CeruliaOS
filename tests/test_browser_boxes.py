"""CSS boxes, final stylesheet base URL, and reduced-scale large baseline JPEG."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import threading
import time

from test_boot_compat import ROOT, boot, ovmf_x64_fd
from test_desktop_accounts import key, screenshot, state

PAGE = b"""<html><head><link rel="stylesheet" href="/alias"></head>
<body><div class="hero"></div><div class="secret">Hidden</div>
<div class="row"><div class="left">Left</div><div class="right">Right</div></div>
<p>After the row</p></body></html>"""
CSS = b"""html {font-size:calc(50 / 250 * 100vw);}
body {background-color:#192139;font-size:16px}
.hero {width:55%;height:0.65rem;margin:0 auto;background: url('../covers/banner.jpg') no-repeat center/cover;}
.secret {display:none;background-image:url('../never.jpg');height:80px;}
.row {width:80%;margin:0 auto;display:flex;gap:20px;}
.left {width:40%;min-height:50px;background:#1f70ad;}
.right {min-height:60px;background:#ed9e38;}
"""
ANCHOR = (b'<link rel=stylesheet href=/late.css><div class=spacer></div>'
          b'<div id=target>Target section</div><div class=tail></div>')
LATE = b'.spacer{height:1600px;}#target{height:90px;background:#8153cd;}.tail{height:1100px;}'


def verify(image, baseline=False, live=False, cdn_jpeg=False):
    paths = []
    fixture = (ROOT / "tests/fixtures/browser/large-baseline.jpg").read_bytes()
    out = ROOT / "temp/browser-boxes"
    out.mkdir(parents=True,exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            if self.path=="/alias":
                self.send_response(302)
                self.send_header("Location","/css/theme/site.css")
                self.send_header("Content-Length","0")
                self.end_headers()
                return
            data,kind = {
                "/start": (PAGE,"text/html"), "/css/theme/site.css": (CSS,"text/css"),
                "/css/covers/banner.jpg": (fixture,"image/jpeg"),
                "/anchor": (ANCHOR,"text/html"), "/late.css": (LATE,"text/css"),
            }.get(self.path,(b"missing","text/plain"))
            if self.path=="/late.css":time.sleep(.7)
            self.send_response(200 if kind!="text/plain" else 404)
            self.send_header("Content-Type",kind)
            self.send_header("Content-Length",str(len(data)))
            self.end_headers()
            if self.path=="/css/covers/banner.jpg" and paths.count(self.path)==1:
                self.wfile.write(data[:128])
            else:
                self.wfile.write(data)

    with ThreadingHTTPServer(("127.0.0.1",0),Handler) as srv:
        thread=threading.Thread(target=srv.serve_forever,daemon=True)
        thread.start()
        try:
            def guest(q,s,scratch):
                q.type_line("desktop")
                time.sleep(4)
                key(q,"b")
                def go(url):
                    key(q,"f1")
                    q.type_line(url)
                def wait(pattern, seconds=100):
                    end=time.monotonic()+seconds
                    while time.monotonic()<end:
                        start=s.size()
                        try:state(q,s)
                        except AssertionError as error:
                            raise AssertionError(s.tail_from(max(0,s.size()-7000))) from error
                        report=s.tail_from(start)
                        if re.search(pattern,report):return report
                        time.sleep(.2)
                    raise AssertionError(report)
                go(f"http://10.0.2.2:{srv.server_port}/start")
                if baseline:
                    wait(r"browser status=200 .* css=1 ")
                    time.sleep(2)
                    assert paths==["/start","/alias","/css/theme/site.css"],paths
                    print("BASELINE: stylesheet fetched; no CSS background request",flush=True)
                    return
                rep=wait(r"css=1 bg=1 ")
                assert paths==["/start","/alias","/css/theme/site.css","/css/covers/banner.jpg","/css/covers/banner.jpg"],paths
                x,y,w,h=map(int,re.search(r"app=8 x=(\d+) y=(\d+) w=(\d+) h=(\d+)",rep).groups())
                for _ in range(10):
                    time.sleep(.4)
                    stride,_,data=screenshot(q,out/"fixture.ppm")
                    def rgb(px,py):
                        return tuple(data[(py*stride+px)*3:(py*stride+px+1)*3])
                    def red(c):return c[0]>225 and c[1]<55 and c[2]<65
                    def green(c):return c[0]<65 and c[1]>220 and c[2]<105
                    if red(rgb(x+w*35//100,y+83)) and green(rgb(x+w*65//100,y+83)):break
                else:raise AssertionError("missing RGB565 pixels from the large background")
                assert rgb(x+w//10,y+83)==(25,33,57)
                assert red(rgb(x+w*35//100,y+140)) and not red(rgb(x+w*35//100,y+240))
                left, right=(31,112,173),(237,158,56)
                rows=[v for v in range(y+150,y+260)
                      if rgb(x+w//5,v)==left and rgb(x+4*w//5,v)==right]
                assert len(rows)>=45,"flex items not painted side by side"
                row=rows[len(rows)//2]
                assert rgb(x+w*43//100,row)==(25,33,57),"flex gap missing"
                key(q,"f8");time.sleep(.55)
                stride,_,data=screenshot(q,out/"reader.ppm")
                assert not red(rgb(x+w*35//100,y+83))
                print("PASS final stylesheet URL, large raster, box centering, viewport/rem sizing, row flex/gap, reader toggle, no hidden fetch",flush=True)
                key(q,"f8")
                go(f"http://10.0.2.2:{srv.server_port}/anchor#target")
                wait(fr"browser status=200 bytes={len(ANCHOR)} .* css=1 ")
                for _ in range(10):
                    time.sleep(.45)
                    stride,_,data=screenshot(q,out/"anchor.ppm")
                    if rgb(x+w//2,y+52)==(129,83,205):break
                else:raise AssertionError("late stylesheet lost the fragment scroll position")
                assert "/anchor" in paths
                print("PASS fragment stays at target after asynchronous stylesheet changes preceding height by 1600px",flush=True)
                if live:
                    go("https://df.qq.com/cp/a20240906main/index.html")
                    r=wait(r"browser status=200 .* css=2 bg=[1-9]\d* ",160)
                    for _ in range(20):
                        time.sleep(.5)
                        sw,_,frame=screenshot(q,out/"df-main.ppm")
                        off=(sw*(y+24)+x+w-55)*3
                        if min(frame[off:off+3])>235:break
                    else:raise AssertionError("complete browser frame not captured")
                    print("PASS actual df.qq.com CSS background",re.search(r"browser status=.*",r)[0],flush=True)
                if cdn_jpeg:
                    go("https://game.gtimg.cn/images/dfm/cp/a20240906main/part2.jpg")
                    r=wait(r"browser status=200 bytes=\d{5,} nodes=1 .*images=1 ",100)
                    print("PASS actual Tencent CDN 4K baseline JPEG",re.search(r"browser status=.*",r)[0],flush=True)
            boot("native normal-flow boxes / x64 UEFI USB only",
                 ["-machine","q35","-cpu","max","-m","256",
                  "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),
                  "-netdev","user,id=n0","-device","e1000,netdev=n0"],
                 ["EZEFI:x64 entering i686"],image=image,usb_boot=True,after=guest)
        finally:
            srv.shutdown()
            thread.join()


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--image",type=Path,default=ROOT/"usb-image.bin")
    p.add_argument("--baseline",action="store_true")
    p.add_argument("--live",action="store_true")
    p.add_argument("--cdn-jpeg",action="store_true",help="also probe the CDN's direct JPEG URL")
    a=p.parse_args()
    verify(a.image.resolve(),a.baseline,a.live,a.cdn_jpeg)
