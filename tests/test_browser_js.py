"""Drive the embedded Ring-3 QuickJS renderer from an emulated USB boot."""
import argparse
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import threading
import time
from test_boot_compat import ROOT, boot, ovmf_x64_fd
from test_desktop_accounts import key, screenshot, state

PAGE=b'<div id="demo">BEFORE-RUN</div><script src="/one.js?v=1&amp;lang=zh"></script><script src="/two.js" defer></script>'
BEFORE=b'<div id="demo">BEFORE-RUN</div>'
AFTER=b'<div id="demo">RING3-42</div>'
ONE=b"""const values=[2,5,9]; const factor=Math.sqrt(4);
if(typeof require!=='undefined'||typeof fetch!=='undefined')throw Error('Unexpected capabilities');
if(document.getElementById('demo').textContent!=='BEFORE-RUN'||document.querySelector('#missing')!==null)
  throw Error('DOM snapshot mismatch');"""
TWO=b"""document.addEventListener('DOMContentLoaded', async () => {
 const x=await Promise.resolve(values.at(1)+values.at(2));
 document.querySelector('#demo').textContent='RING3-'+(x*factor+14);
});"""
LOOP=b"document.getElementById('demo').textContent='UNCOMMITTED'; for (;;) {}"
CSP=BEFORE+b'<script src="/never.js"></script>'


def test(image, baseline=False):
    seen=[]
    out=ROOT/"temp/quickjs"
    out.mkdir(parents=True,exist_ok=True)
    class Peer(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body,kind={
                "/page": (PAGE,"text/html"),"/reference":(AFTER,"text/html"),
                "/before":(BEFORE,"text/html"),"/one.js?v=1&lang=zh":(ONE,"text/javascript"),
                "/two.js":(TWO,"application/javascript"),"/loop.js":(LOOP,"text/javascript"),
                "/slow":(BEFORE+b'<script src="/loop.js"></script>',"text/html"),
                "/csp":(CSP,"text/html"),
            }.get(self.path,(b"no resource","text/plain"))
            self.send_response(200)
            self.send_header("Content-Type",kind)
            if self.path=="/csp":self.send_header("Content-Security-Policy","script-src 'none'")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers();self.wfile.write(body)
    with ThreadingHTTPServer(("127.0.0.1",0),Peer) as server:
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        base=f"http://10.0.2.2:{server.server_port}"
        def guest(q,s,scratch):
            q.type_line("desktop")
            time.sleep(4)
            key(q,"b")
            def go(path):
                key(q,"f1")
                q.type_line(base+path)
            def wait(pattern,seconds=55):
                until=time.monotonic()+seconds
                while time.monotonic()<until:
                    start=s.size();state(q,s);rep=s.tail_from(start)
                    if re.search(pattern,rep):return rep
                    time.sleep(.35)
                raise AssertionError(rep+"\n"+s.tail_from(max(0,s.size()-3800)))
            if baseline:
                go("/page");wait(rf"browser status=200 bytes={len(PAGE)} ")
                time.sleep(1.5);assert seen==["/page"],seen
                print("BASELINE: external scripts were not requested",flush=True);return
            def capture(name,report):
                x,y,w,h=map(int,re.search(r"app=8 x=(\d+) y=(\d+) w=(\d+) h=(\d+)",report).groups())
                for _ in range(10):
                    time.sleep(.46)
                    stride,_,pixels=screenshot(q,out/(name+".ppm"))
                    rows=[pixels[(r*stride+x+1)*3:(r*stride+x+1+160)*3] for r in range(y+39,y+110)]
                    colored=[r for r in rows if any(r[k:k+3]==b"\0\0\0" for k in range(0,len(r),3))]
                    if len(colored)>=5:return colored
                raise AssertionError("guest browser body not painted")
            go("/reference")
            token=capture("reference",wait(rf"browser status=200 bytes={len(AFTER)} "))
            go("/before")
            prior=capture("prior",wait(rf"browser status=200 bytes={len(BEFORE)} "))
            go("/page")
            rep=wait(rf"browser status=200 bytes={len(PAGE)} .*js=2 jserr=\d+ ")
            assert "jserr=0 " in rep,rep
            assert seen[-3:]==["/page","/one.js?v=1&lang=zh","/two.js"],seen
            assert capture("scripted",rep)==token
            print("PASS two fetched scripts share a QuickJS global; DOM read, Math, async/await and DOM-ready update painted RING3-42",flush=True)
            go("/slow")
            rep=wait(r"js=1 jserr=[1-9]\d* ")
            assert "jserr=124 " in rep,rep
            assert capture("interrupted",rep)==prior
            print("PASS infinite script interrupted, desktop responsive, uncommitted DOM changes discarded",flush=True)
            go("/csp")
            wait(rf"browser status=200 bytes={len(CSP)} .*js=0 jserr=0 ")
            time.sleep(1)
            assert "/never.js" not in seen
            print("PASS CSP kept external script from being fetched",flush=True)
        try:
            trace=["-d","int","-D",str(out/"interrupts.log")] if os.getenv("CERULIA_WEBJS_TRACE") else []
            boot("QuickJS renderer / x64 UEFI USB only",["-machine","q35","-cpu","max","-m","256",*trace,
                 "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),
                 "-netdev","user,id=n0","-device","e1000,netdev=n0"],
                 ["EZEFI:x64 entering i686"],image=image,usb_boot=True,after=guest)
        finally:
            server.shutdown();thread.join()


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--baseline",action="store_true")
    p.add_argument("--image",type=Path,default=ROOT/"usb-image.bin")
    a=p.parse_args()
    test(a.image.resolve(),a.baseline)
