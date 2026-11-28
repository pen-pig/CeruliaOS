"""Native form POST and cookies over TLS in a disposable USB boot image.

Use an ephemeral CA and two local test names. The production BearSSL archive,
trust store, kernel, ESP and boot images are never rewritten.
"""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time

from test_boot_compat import ROOT, boot, ovmf_x64_fd
from test_desktop_accounts import click_at, key, screenshot, state
from test_nvme import KEYMAP
from ezos_env import tool
from tools import build_tls, make_usb_image as usb

LOGIN = b"""<form method="post" action="/auth/start">
  <input name="user" type="email" required>
  <input name="secret" type="password" required>
  <input type="hidden" name="csrf" value="two&amp;parts">
  <button type="submit">Sign in</button>
</form>"""
FORWARD = b'<form method="POST" action="/auth/leak"><input name=q required></form>'
IDENTITY = "alice+team@example.test"
SECRET = "test&only 123"
WIRE = b"user=alice%2Bteam%40example.test&secret=test%26only+123&csrf=two%26parts"


def certificates(work):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    ca_key=ec.generate_private_key(ec.SECP256R1())
    key=ec.generate_private_key(ec.SECP256R1())
    ca_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"Cerulia isolated browser test CA")])
    subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"login.browser.test")])
    now=datetime.now(timezone.utc)
    def cert(name,pub):
        return (x509.CertificateBuilder().subject_name(name).issuer_name(ca_name)
                .public_key(pub).serial_number(x509.random_serial_number())
                .not_valid_before(now-timedelta(days=2)).not_valid_after(now+timedelta(days=2)))
    ca=cert(ca_name,ca_key.public_key()).add_extension(x509.BasicConstraints(ca=True,path_length=0),True).sign(ca_key,hashes.SHA256())
    leaf=(cert(subject,key.public_key()).add_extension(x509.BasicConstraints(ca=False,path_length=None),True)
          .add_extension(x509.SubjectAlternativeName([x509.DNSName("login.browser.test"),x509.DNSName("other.browser.test")]),False)
          .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),False).sign(ca_key,hashes.SHA256()))
    pem=work/"leaf-and-key.pem"
    pem.write_bytes(leaf.public_bytes(serialization.Encoding.PEM)+key.private_bytes(serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    return ca.public_bytes(serialization.Encoding.PEM),pem


def fixture_usb(work,ca):
    cc=tool("cc")
    headers=ROOT/"temp/tls-build/bearssl-0.6"
    (work/"roots.h").write_text(build_tls.roots(ca),encoding="ascii")
    archive=work/"fixture-tls.a"
    shutil.copyfile(ROOT/"temp/libcerulia_tls.a",archive)
    # BearSSL, embedded DNS override and certificate are disposable only.
    ar=subprocess.check_output([cc,"-print-prog-name=ar"],text=True).strip()
    subprocess.run([ar,"d",str(archive),"port.o"],check=True)
    port=work/"port.o"
    subprocess.run([cc,"-m32","-march=i686","-Os","-ffreestanding","-fno-pic","-fno-pie","-mno-sse",
                    "-I"+str(ROOT/"third_party/bearssl/compat"),"-I"+str(headers/"inc"),"-I"+str(work),
                    "-c",str(ROOT/"third_party/bearssl/port.c"),"-o",str(port)],check=True)
    subprocess.run([ar,"r",str(archive),str(port)],check=True)
    dns=work/"fixture_dns.c"
    dns.write_text("""
static int equal(const char *a,const char *b){while(*a&&*a==*b){a++;b++;}return *a==*b;}
int __real_net_dns_resolve(const char*,unsigned*,unsigned);
int __wrap_net_dns_resolve(const char *name,unsigned *addr,unsigned wait){
    if(equal(name,"login.browser.test")||equal(name,"other.browser.test")||equal(name,"wrong.browser.test")){
        *addr=0x0a000202u;return 0;
    }
    return __real_net_dns_resolve(name,addr,wait);
}
""",encoding="ascii")
    dnsobj=work/"fixture_dns.o"
    subprocess.run([cc,"-m32","-Os","-ffreestanding","-fno-pic","-fno-pie","-c",dns,"-o",dnsobj],check=True)
    import shlex
    link = subprocess.check_output([str(ROOT/"ninja.exe"),"-f","build_auto.ninja","-t","commands",
                                    "kernel_raw.bin"],cwd=ROOT,text=True).splitlines()[-1]
    args=shlex.split(link)
    assert args[args.index("-o")+1]=="kernel_raw.bin"
    raw=work/"fixture-raw.bin"
    args[args.index("-o")+1]=str(raw)
    args[args.index("temp/libcerulia_tls.a")]=str(archive)
    args += ["--wrap=net_dns_resolve",str(dnsobj)]
    subprocess.run(args,cwd=ROOT,check=True)
    kernel,os_image=work/"kernel.bin",work/"os-image.bin"
    subprocess.run([sys.executable,"tools/make_image.py","boot/boot.bin","boot/stage2.bin",raw,kernel,os_image],
                   cwd=ROOT,check=True)
    total=usb.ESP_LBA+usb.ESP_SECTORS
    img=bytearray(total*usb.SECTOR)
    img[:usb.SECTOR]=usb.patch_mbr((ROOT/"boot/mbr_usb.bin").read_bytes(),total,usb.VBR_LBA,usb.ESP_LBA,usb.ESP_SECTORS)
    ez=os_image.read_bytes()
    img[usb.SECTOR:len(ez)]=ez[usb.SECTOR:]
    img[usb.VBR_LBA*usb.SECTOR:(usb.VBR_LBA+1)*usb.SECTOR]=ez[:usb.SECTOR]
    esp=ROOT/"uefi/esp/EFI/BOOT"
    img[usb.ESP_LBA*usb.SECTOR:]=usb.build_esp(usb.ESP_SECTORS,kernel.read_bytes(),
               (esp/"BOOTIA32.EFI").read_bytes(),(esp/"BOOTX64.EFI").read_bytes())[0]
    out=work/"test-only-usb.bin"
    out.write_bytes(img)
    usb.check_image(out,require_x64=True,kernel_source=kernel)
    return out


def verify():
    with tempfile.TemporaryDirectory(prefix="cerulia-session-test-") as tmp:
        work=Path(tmp)
        ca,pem=certificates(work)
        image=fixture_usb(work,ca)
        seen=[]
        reply_sizes={}
        class Peer(BaseHTTPRequestHandler):
            def log_message(self,format,*args):
                print(self.command,self.path,flush=True)
            def reply(self,body,code=200,headers=()):
                reply_sizes[self.path]=len(body)
                self.send_response(code)
                for k,v in headers:self.send_header(k,v)
                self.send_header("Content-Type","text/html")
                self.send_header("Content-Length",str(len(body)))
                self.end_headers()
                if body:self.wfile.write(body)
            def do_GET(self):
                c=self.headers.get("Cookie","")
                seen.append(("GET",self.path,self.headers.get("Host"),c,None,None))
                if self.path=="/login":
                    self.reply(LOGIN,headers=[
                        ("Set-Cookie","csrf=preflight; Path=/; Secure; HttpOnly"),
                        ("Set-Cookie","bad=reject; Domain=example.test; Path=/"),
                        ("Set-Cookie","__Host-invalid=no; Secure; Path=/incorrect"),
                        ("Set-Cookie","expired=gone; Path=/; Expires=Thu, 01 Jan 1970 00:00:01 GMT")])
                elif self.path=="/jump":
                    self.reply(b"",302,[("Location",f"https://other.browser.test:{srv.server_port}/foreign")])
                elif self.path=="/auth/result":
                    self.reply(b"Authenticated" if "sid=ticket" in c else b"No session here")
                elif self.path=="/auth/clear":
                    self.reply(b"Signed out",headers=[("Set-Cookie","sid=0; Path=/auth; Max-Age=0; Secure")])
                elif self.path=="/form307":
                    self.reply(FORWARD)
                elif self.path=="/outside":
                    self.reply(b"Outside cookie path")
                elif self.path=="/foreign":
                    self.reply(b"Different origin")
                else:self.reply(b"Unknown browser page")
            def do_POST(self):
                body=self.rfile.read(int(self.headers["Content-Length"]))
                seen.append(("POST",self.path,self.headers.get("Host"),self.headers.get("Cookie",""),
                             self.headers.get("Origin"),body))
                if self.path=="/auth/start":
                    self.reply(b"",303,[("Location","/auth/result"),
                                       ("Set-Cookie","sid=ticket; Domain=.browser.test; Path=/auth; Max-Age=120; Secure; HttpOnly")])
                elif self.path=="/auth/leak":
                    self.reply(b"",307,[("Location",f"https://other.browser.test:{srv.server_port}/never")])
                else:self.reply(b"Unexpected method",400)

        with ThreadingHTTPServer(("127.0.0.1",0),Peer) as srv,ThreadingHTTPServer(("127.0.0.1",0),Peer) as plain:
            ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version=ctx.maximum_version=ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(pem)
            srv.socket=ctx.wrap_socket(srv.socket,server_side=True)
            thread=threading.Thread(target=srv.serve_forever,daemon=True)
            plain_thread=threading.Thread(target=plain.serve_forever,daemon=True)
            thread.start()
            plain_thread.start()
            base=f"https://login.browser.test:{srv.server_port}"
            host=f"login.browser.test:{srv.server_port}"
            def guest(q,s,scratch):
                q.type_line("desktop")
                time.sleep(4)
                key(q,"b")
                def go(url):
                    key(q,"f1")
                    q.type_line(url)
                def wait(pattern,seconds=50):
                    until=time.monotonic()+seconds
                    while time.monotonic()<until:
                        start=s.size();state(q,s);rep=s.tail_from(start)
                        if re.search(pattern,rep):return rep
                        time.sleep(.2)
                    raise AssertionError(rep)
                def probe(url,pattern):
                    # Only retry an idempotent GET if the emulated network
                    # times out before a successful response; never retry POST.
                    for _ in range(3):
                        go(url)
                        report=wait(pattern+r"|browser status=0 .*err=(?:2|3|4|5|10) ")
                        if re.search(pattern,report):return report
                        print("RETRY GET on guest transport timeout",flush=True)
                    raise AssertionError(report)
                rep=probe(base+"/login",rf"browser status=200 bytes={len(LOGIN)} .*form=1")
                x,y=map(int,re.search(r"app=8 x=(\d+) y=(\d+)",rep).groups())
                key(q,"tab");q.type_line(IDENTITY)
                for c in SECRET:
                    q.hmc("sendkey "+KEYMAP.get(c,c))
                    time.sleep(.055)
                p=ROOT/"temp/browser-session"
                p.mkdir(parents=True,exist_ok=True)
                path=p/"password-mask.ppm"
                for _ in range(10):
                    time.sleep(.35)
                    w,_,rgb=screenshot(q,path)
                    def cell(i):
                        col=x+9+i*8
                        return b"".join(rgb[3*(w*r+col):3*(w*r+col+8)] for r in range(y+72,y+80))
                    glyphs=[cell(i) for i in range(len(SECRET))]
                    if len(set(glyphs))==1 and len(set(glyphs[0]))>1:break
                else:raise AssertionError("Password did not paint as repeated masked glyphs")
                key(q,"ret")
                wait(r"browser status=200 bytes=13 nodes=1")
                assert seen[-2]==("POST","/auth/start",host,"csrf=preflight",base,WIRE),seen[-2]
                assert seen[-1][:4]==("GET","/auth/result",host,"sid=ticket; csrf=preflight"),seen[-1]
                assert SECRET not in s.tail_from(0)
                print("PASS native password form POST, byte encoding, Origin, 303 GET, path-prioritized cookies",flush=True)
                go(base+"/outside")
                wait(r"browser status=200 bytes=19 nodes=1")
                assert seen[-1][3]=="csrf=preflight"
                go(base+"/jump")
                wait(r"browser status=200 bytes=16 nodes=1")
                assert seen[-1][1]=="/foreign" and seen[-1][3]==""
                go(base+"/auth/clear")
                wait(r"browser status=200 bytes=10 nodes=1")
                go(base+"/auth/result")
                wait(r"browser status=200 bytes=15 nodes=1")
                assert 'sid=' not in seen[-1][3]
                go(base+"/form307")
                wait(rf"browser status=200 bytes={len(FORWARD)} .*form=1")
                key(q,"tab");q.type_line("not for other origins")
                rep=wait(r"browser status=307 .*err=14 ")
                assert all(x[1]!="/never" for x in seen)
                x,y,w=map(int,re.search(r"app=8 x=(\d+) y=(\d+) w=(\d+)",rep).groups())
                click_at(q,s,x+w-7,y+6)
                key(q,"b")
                go(base+"/outside")
                wait(r"browser status=200 bytes=19 nodes=1")
                assert not seen[-1][3]
                print("PASS expiry, origin/path isolation, cross-origin 307 refused, cookie jar cleared on close",flush=True)
                go(f"http://login.browser.test:{plain.server_port}/login")
                wait(rf"browser status=200 bytes={len(LOGIN)} .*form=0")
                assert not seen[-1][3]
                print("PASS plaintext page gets neither a credential form nor a Cookie header",flush=True)
                count=len(seen)
                probe(f"https://wrong.browser.test:{srv.server_port}/wrong",r"browser status=0 .*err=13 ")
                assert len(seen)==count
                print("PASS wrong certificate hostname rejected before HTTP",flush=True)
            try:
                boot("private TLS fixture from USB only",["-machine","q35","-cpu","max","-m","256",
                     "-drive","if=pflash,format=raw,readonly=on,file="+ovmf_x64_fd(),
                     "-netdev","user,id=n0","-device","e1000,netdev=n0"],
                     ["EZEFI:x64 entering i686"],image=image,usb_boot=True,after=guest)
            finally:
                srv.shutdown()
                plain.shutdown()
                thread.join()
                plain_thread.join()


if __name__=="__main__":
    verify()
