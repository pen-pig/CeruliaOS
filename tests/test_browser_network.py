"""Real desktop while an HTTP peer stalls; optional public HTTPS acceptance."""
import argparse
import datetime
import gzip
import re
import ssl
import socketserver
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from test_accounts import (LOGIN, ROOT_PROMPT, SerialReader, Qmp, ROOT, alloc_port,
                           gen_diskimg, image_path, inputs, qemu_exe, wait_for)
from test_desktop_accounts import key, state, screenshot


class Peer(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(15)
        req = b""
        while b"\r\n\r\n" not in req:
            block = self.request.recv(2048)
            if not block:
                return
            req += block
        if req.startswith(b"GET /slow "):
            self.server.started.set()
            self.server.release.wait(12)
        body=b"NET-PASS!"
        if req.startswith(b"GET /a/b/start "):
            dest="../中文?ticket=".encode()+b"x"*420+b"#fragment"
            self.request.sendall(b"HTTP/1.1 302 Found\r\nLocation: "+dest+b"\r\nContent-Length: 0\r\n\r\n")
            return
        if req.startswith(b"GET /a/"):
            self.server.redirected=req.split(b" ",2)[1]
            body=b"REDIRECT-PASS!"
        if req.startswith(b"GET /?root=1 "):
            body=b"ROOTQUERY-PASS!"
        if req.startswith(b"GET /large "):
            body=b"<!--"+b"x"*211000+b"--><p>Completed</p>"
            self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nTransfer-Encoding: chunked\r\n\r\n")
            for i in range(0,len(body),12000):
                chunk=body[i:i+12000]
                self.request.sendall(f"{len(chunk):x}\r\n".encode()+chunk+b"\r\n")
            self.request.sendall(b"0\r\n\r\n")
            return
        charset_cases={
            b"/legacy":("", "gbk", 'text/html; charset = "GBK"'),
            b"/meta-gbk":('<!-- <meta charset=utf-8> --><script>"<meta charset=utf-8>"</script>'
                         '<META Charset="GBK" />', "gbk", "text/html"),
            b"/meta-pragma":('<meta content="text/html; charset=gb2312" http-equiv=Content-Type>',
                             "gb2312", "text/html"),
            b"/meta-precedence":('<meta charset=gbk>', "utf-8", "text/html; charset=utf-8"),
            b"/meta-bom":('<meta charset=gbk>', "utf-8-sig", "text/html; charset=gbk"),
        }
        charset_case=charset_cases.get(req.split(b" ",2)[1])
        if req.startswith(b"GET /form "):
            body=b"""<form action="/actual?obsolete=1" onsubmit="const t=document.getElementById('term').value.trim().toUpperCase();document.getElementById('term').value=t + '!';return true;">
            <input type="search" name="word" id="term"><input type="hidden" name="mode" value="a&amp;b"></form>
            <script>document.getElementById('term').value = 'js ';</script>"""
        if req.startswith(b"GET /actual?"):
            self.server.received=req.split(b" ",2)[1]
            self.server.searched.set()
            body=b'<h2><a target="_blank" href=/article>Article</a></h2>'
        if req.startswith(b"GET /article "):
            self.server.article.set()
        mime=b"text/plain" if body in (b"REDIRECT-PASS!",b"ROOTQUERY-PASS!") or req.startswith(b"GET /ok ") else b"text/html"
        if charset_case:
            head,enc,ctype=charset_case
            title="龘繁體中文" if enc!="gb2312" else "中文"
            body=(head+f"<title>{title}</title><p>网页</p>").encode(enc)
            mime=ctype.encode("ascii")
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: "+mime+b"\r\nContent-Length: "
                             +str(len(body)).encode()+b"\r\n\r\n"+body)


def tls_peer(work):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    priv = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME,"10.0.2.2")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(priv.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-datetime.timedelta(days=2)).not_valid_after(now+datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("10.0.2.2")]),False)
            .sign(priv,hashes.SHA256()))
    pem=work/"server.pem"
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM)+priv.private_bytes(
        serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(pem)
    class Untrusted(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                with ctx.wrap_socket(self.request,server_side=True) as conn:
                    if conn.recv(512): self.server.saw_http.set()
            except (OSError, ssl.SSLError):
                pass
    srv=socketserver.ThreadingTCPServer(("127.0.0.1",0),Untrusted)
    srv.saw_http=threading.Event()
    return srv


def run(work, server, bad, live, launcher, df, luogu):
    qp, sp = alloc_port(2)
    disk = work / "data.vhd"
    gen_diskimg.gen_exfat(disk)
    # Connect directly to SLiRP's host alias (unlike guestfwd it supports
    # multiple connections to a listening service).
    base = f"http://10.0.2.2:{server.server_address[1]}"
    flags = ["-cpu", "max", "-m", "128", "-netdev", "user,id=n0","-device", "e1000,netdev=n0"]
    if launcher:
        # Derive the machine/NIC/clock flags from the exact Windows entry point.
        from shlex import split
        line = subprocess.check_output([str(ROOT/"ninja.exe"),"-f","build_auto.ninja",
                                        "-t","commands","run"], cwd=ROOT,text=True).splitlines()[-1]
        flags = split(line)[1:]
        for _ in range(2):
            i=flags.index("-drive")
            del flags[i:i+2]
    cmd = [qemu_exe(), *flags, "-display", "none",
           "-drive", f"format=raw,index=0,file={image_path()},snapshot=on",
           "-drive", f"format=raw,index=1,file={disk}", "-serial", f"tcp:127.0.0.1:{sp},server,wait",
           "-qmp", f"tcp:127.0.0.1:{qp},server,nowait"]
    with subprocess.Popen(cmd, cwd=ROOT) as vm:
        serial = None
        try:
            serial = SerialReader(sp)
            q = Qmp(qp)
            wait_for(serial, 0, lambda s: LOGIN in s)
            inputs(q, serial, "root", ROOT_PROMPT)
            inputs(q, serial, "fetch " + base + "/ok", "NET-PASS!", ROOT_PROMPT)
            print("PASS plain HTTP on e1000", flush=True)
            inputs(q,serial,"fetch "+base+"/a/b/start","REDIRECT-PASS!",ROOT_PROMPT)
            assert server.redirected==b"/a/%E4%B8%AD%E6%96%87?ticket="+b"x"*420, server.redirected
            print("PASS long relative redirect, UTF-8 URL encoding and no HTTP fragment",flush=True)
            inputs(q,serial,"fetch "+base+"?root=1","ROOTQUERY-PASS!",ROOT_PROMPT)
            print("PASS root URL with query and no slash",flush=True)
            if bad:
                inputs(q,serial,f"fetch https://10.0.2.2:{bad.server_address[1]}/",
                       "TLS certificate verification failed",ROOT_PROMPT)
                assert not bad.saw_http.is_set()
                print("PASS untrusted certificate rejected before HTTP GET",flush=True)
            if live:
                start = serial.size()
                q.type_line("fetch https://bing.com/ bing.htm")
                until = time.monotonic() + 70
                while time.monotonic() < until:
                    reply = serial.tail_from(start)
                    if "\n" + ROOT_PROMPT in reply and ("HTTP " in reply or reply.count("fetch:") >= 2):
                        break
                    time.sleep(.1)
                print(reply[-550:], flush=True)
                assert "HTTP 200" in reply, reply[-550:]
            inputs(q, serial, "dhcp", "DHCP: got 10.0.2.15", "DHCP: lease ", ROOT_PROMPT)
            print("PASS manual DHCP lease 10.0.2.15", flush=True)
            q.type_line("desktop")
            time.sleep(4)
            key(q, "b")
            key(q, "f8")  # compare the stable, unstyled reader glyphs
            key(q, "f1")
            def wait_page(pattern, timeout=20):
                end=time.monotonic()+timeout
                while time.monotonic()<end:
                    start=serial.size()
                    state(q,serial)
                    report=serial.tail_from(start)
                    if re.search(pattern,report): return report
                    time.sleep(.25)
                raise AssertionError(report)
            def replace_address():
                key(q,"f1")
            if live:
                q.type_line("bing.com")
                end = time.monotonic() + 60
                while time.monotonic() < end:
                    start = serial.size()
                    state(q,serial)
                    report = serial.tail_from(start)
                    if re.search(r"browser status=200 bytes=[1-9]\d* nodes=[1-9]",report):
                        break
                    time.sleep(.3)
                else:
                    raise AssertionError(report)
                print("PASS address bar bing.com: "+re.search(r"browser status=.*",report)[0],flush=True)
                # Give the compositor time to finish after F12. Inspect only
                # the page, excluding toolbar, borders, cursor and scroll bar.
                m=re.search(r"app=8 x=(\d+) y=(\d+) w=(\d+) h=(\d+)",report)
                x,y,w,h=map(int,m.groups())
                for _ in range(5):
                    time.sleep(.5)
                    width,_,rgb=screenshot(q,ROOT/"temp"/"bing-render.ppm")
                    rows=[rgb[(r*width+x+2)*3:(r*width+x+w-10)*3] for r in range(y+64,min(y+h-4,y+245))]
                    bg=rows[0][-3:]
                    # A horizontal divider alone is not text.
                    if sum(any(row[i:i+3]!=bg for i in range(0,len(row),3)) for row in rows)>5:
                        break
                else: raise AssertionError("HTTP 200 but blank viewport")
                assert "form=1" in report,report
                key(q,"tab")
                q.type_line("cerulia os")
                report=wait_page(r"browser status=200 .*headinglinks=[1-9]",80)
                print("PASS Bing search with clickable heading links: "+re.search(r"browser status=.*",report)[0],flush=True)
                screenshot(q,ROOT/"temp"/"bing-search.ppm")
                replace_address()
            q.type_line(base+"/form")
            wait_page(r"browser status=200 .*form=1")
            key(q,"tab")
            q.type_line("two words")
            assert server.searched.wait(8), "form did not submit"
            assert server.received==b"/actual?word=JS+TWO+WORDS%21&mode=a%26b",server.received
            report=wait_page(r"browser status=200 .*headinglinks=[1-9]")
            print("PASS guest inline JS + onsubmit + escaped GET: "+server.received.decode(),flush=True)
            from test_desktop_accounts import click_at
            m=re.search(r"app=8 x=(\d+) y=(\d+)",report)
            x,y=map(int,m.groups())
            click_at(q,serial,x+15,y+55)
            assert server.article.wait(8),"rendered result link did not open"
            wait_page(r"browser status=200 bytes=9")
            print("PASS clicked search result fetched its actual href",flush=True)
            replace_address()
            q.type_line(base + "/slow")
            assert server.started.wait(7), serial.tail_from(0)[-600:]
            # Guest F12 and opening the start menu both require the desktop task.
            t = time.monotonic()
            assert state(q, serial)[0] == 1
            key(q, "f10")
            assert state(q, serial)[1] == 1
            assert time.monotonic() - t < 4
            server.release.set()
            print("PASS live desktop while server withholds response", flush=True)
            key(q, "f10")  # close the menu; inspect the document, not the menu
            for _ in range(12):
                start=serial.size()
                state(q,serial)
                report=serial.tail_from(start)
                if "browser status=200 bytes=9 nodes=1 " in report:
                    break
                time.sleep(.25)
            else: raise AssertionError(report)
            m=re.search(r"app=8 x=(\d+) y=(\d+) w=(\d+) h=(\d+)",report)
            x,y,w,h=map(int,m.groups())
            # Nine ASCII glyphs at the top-left, with an empty page beyond.
            # Check each character cell: a stale 'Loading...' cannot pass
            # the combination of nine inked cells and a blank tenth.
            for _ in range(8):
                width,_,rgb=screenshot(q,work/"local.ppm")
                def ink(cell):
                    return sum(rgb[(r*width+x+1+cell*8+c)*3:(r*width+x+1+cell*8+c)*3+3]
                               !=rgb[(r*width+x+150)*3:(r*width+x+150)*3+3]
                               for r in range(y+38,y+58) for c in range(8))
                if all(ink(i)>0 for i in range(9)) and ink(9)==0:
                    break
                time.sleep(.25)
            else: raise AssertionError("Local response not visible in desktop viewport")
            print("PASS local response rendered after delayed request",flush=True)
            q.hmc("mouse_move 80 56")  # move the cursor away from the sampled glyph
            wanted={ord(c) for c in "龘中角洲进入超级玛丽题库洛谷描述"}
            glyphs={}
            with gzip.open(ROOT/"third_party/unifont/unifont-17.0.04.hex.gz","rt") as font:
                for line in font:
                    code,raw=line.split(":")
                    if int(code,16) in wanted: glyphs[chr(int(code,16))]=bytes.fromhex(raw)

            def title_check(url, chars, shot, search=False, previous=None):
                if url:
                    replace_address()
                    q.type_line(url)
                if previous:
                    old_bytes=re.search(r"browser status=200 bytes=(\d+) ",previous)[1]
                    wait_page(r"browser status=200 bytes=(?!"+old_bytes+r" )[1-9]\d* nodes=[1-9]",55)
                report=wait_page(r"browser status=200 bytes=[1-9]\d* nodes=[1-9]",55)
                x,y,w=map(int,re.search(r"app=8 x=(\d+) y=(\d+) w=(\d+)",report).groups())
                x+=7; y+=43  # frame + toolbar + title margin
                for _ in range(8):
                    time.sleep(.25)
                    width,_,rgb=screenshot(q,shot)
                    def matches(pos,glyph):
                        for r in range(16):
                            bg_off=(width*(y+r)+x+w-30)*3
                            bg=rgb[bg_off:bg_off+3]
                            for c in range(16):
                                off=(width*(y+r)+pos+c)*3
                                if (rgb[off:off+3]!=bg)!=bool(glyph[r*2+c//8] & (128>>(c%8))):
                                    return False
                        return True
                    places=range(x,x+w-38,8) if search else (x,)
                    if all(any(matches(pos,glyphs[c]) for pos in places) for c in chars):
                        return report
                raise AssertionError(f"{url}: title did not render expected glyphs {chars!r}")

            def click_label(report, text, shot, blue=True):
                """Find the site's own painted label; do not synthesize the next URL."""
                from test_desktop_accounts import click_at
                x,y,w,h=map(int,re.search(r"app=8 x=(\d+) y=(\d+) w=(\d+) h=(\d+)",report).groups())
                # The previous click can leave the cursor covering the same
                # coordinates on the newly loaded page, ruining exact masks.
                for _ in range(3):
                    q.hmc("mouse_move 110 45")
                    time.sleep(.06)
                time.sleep(.5)
                width,_,rgb=screenshot(q,shot)
                masks=[int.from_bytes(b"".join(glyphs[ch][r*2:r*2+2] for ch in text),"big") for r in range(16)]
                step=len(text)*16
                ones=[[i for i in range(step) if bits & (1<<(step-i-1))] for bits in masks]
                start_row=max(range(16),key=lambda r: len(ones[r]))
                def pixel(row,col):
                    off=(row*width+col)*3
                    r,g,b=rgb[off:off+3]
                    return r<50 and (b>100 if blue else max(r,g,b)<90)
                order=[start_row,*[r for r in range(16) if r!=start_row]]
                for row in range(y+37,y+h-20):
                    for col in range(x+1,min(x+410,x+w-step-6)):
                        if not all(pixel(row+start_row,col+c) for c in ones[start_row][:3]): continue
                        if all(sum(pixel(row+r,col+c)<<(step-c-1) for c in range(step))==masks[r] for r in order):
                            if blue: click_at(q,serial,col+4,row+8)
                            return
                raise AssertionError(f"could not find painted label {text!r}")

            for path,ch in [("/legacy","龘"), ("/meta-gbk","龘"), ("/meta-pragma","中"),
                            ("/meta-precedence","龘"), ("/meta-bom","龘")]:
                title_check(base+path,ch,work/"charset.ppm")
                print(f"PASS {path}: title glyph U+{ord(ch):04X}",flush=True)
            replace_address()
            q.type_line(base+"/large")
            wait_page(r"browser status=200 bytes=211023 nodes=[1-9]",75)
            print("PASS complete 211 KB chunked response",flush=True)
            if df:
                report=title_check("https://df.qq.com/","角洲",ROOT/"temp"/"df-charset.ppm",search=True)
                print("PASS live df.qq.com: exact Chinese title glyphs over HTTPS",flush=True)
                click_label(report,"进入",work/"df-entry.ppm")
                title_check(None,"角洲",ROOT/"temp"/"df-main-guest.ppm",search=True,previous=report)
                print("PASS live df.qq.com: clicked 进入官网 and rendered the UTF-8 title",flush=True)
            if luogu:
                title_check("https://luogu.com.cn/","洛谷",work/"luogu-home.ppm",search=True)
                report=title_check("https://www.luogu.com.cn/problem/list","题库",work/"luogu-list.ppm",search=True)
                click_label(report,"超级",work/"luogu-list.ppm")
                title_check(None,"玛丽",ROOT/"temp"/"luogu-problem-guest.ppm",search=True,previous=report)
                print("PASS live Luogu: home, problem list and click into P1000",flush=True)
                report=title_check("https://www.luogu.com.cn/problem/P1001","洛谷",ROOT/"temp"/"luogu-p1001.ppm",search=True)
                click_label(report,"描述",ROOT/"temp"/"luogu-p1001.ppm",blue=False)
                print("PASS live Luogu: rendered the P1001 problem statement heading",flush=True)
        finally:
            server.release.set()
            if serial:
                serial.close()
            vm.kill()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="also require HTTPS 200 from public Bing")
    ap.add_argument("--launcher", action="store_true", help="use generated Windows Ninja run flags")
    ap.add_argument("--df", action="store_true", help="also verify the public df.qq.com homepage and main site")
    ap.add_argument("--luogu", action="store_true", help="also verify the public Luogu problem list and statements")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="cerulia-web-") as tmp, socketserver.ThreadingTCPServer(
            ("127.0.0.1", 0), Peer) as server:
        server.started, server.release = threading.Event(), threading.Event()
        server.searched,server.article=threading.Event(),threading.Event()
        try:
            bad=tls_peer(Path(tmp))
        except ImportError:
            bad=None
            print("SKIP untrusted peer (install cryptography for this test)")
        servers=[s for s in (server,bad) if s]
        threads=[threading.Thread(target=s.serve_forever) for s in servers]
        for th in threads: th.start()
        try:
            run(Path(tmp), server, bad, args.live, args.launcher, args.df, args.luogu)
        finally:
            for s in servers: s.shutdown()
            for th in threads: th.join()
            if bad: bad.server_close()
