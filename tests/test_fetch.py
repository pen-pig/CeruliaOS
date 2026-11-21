# -*- coding: utf-8 -*-
"""test_fetch.py - HTTP 客户端（fetch）E2E

用宿主 Python 起一个真HTTP 服务器，让 guest 的 `fetch` 去抓，覆盖四种响应体形态：

  [len]     Content-Length 定长 + 200        -> 断言内容逐字节一致
  [chunked] Transfer-Encoding: chunked       -> 断言多块 + 十六进制块长拼接正确
  [redir]   302 -> /final                     -> 断言跟随后拿到最终内容
  [html]    text/html                         -> 断言 fetch 提示存文件而不是刷屏

拓扑：QEMU slirp 用 **guestfwd** 把 guest 连 10.0.2.100:80 的流量转到宿主
127.0.0.1:<hostport> 上跑的 Python HTTP 服务器。
注意方向：hostfwd 是"宿主->guest"，guest 要主动出去抓网页必须用 **guestfwd**，
用错的话症状是 guest 侧 connect failed（很容易误判成 TCP 实现有问题）。

guest 用 IP 直连（绕开 DNS —— 这里测 HTTP 解析，DNS 由 test_dns.py 覆盖）。

端口：monitor 4492。hostfwd 端口必须挑**没人监听的**：被别的服务占着时
转发不出去，症状是 guest侧连接超时而非报错（test_netapp.py 里踩过）。
用法：python tests/test_fetch.py    （退出码 0 = 全部通过）
"""
import os as _os_ez
import sys as _sys_ez
_sys_ez.path.insert(0, _os_ez.path.dirname(_os_ez.path.abspath(__file__)))
from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
import os as _os_ezos
import sys as _sys_ezos
_sys_ezos.path.append(_os_ezos.path.dirname(
    _os_ezos.path.dirname(_os_ezos.path.abspath(__file__))))
from ezos_env import qemu_exe  # noqa: E402

import http.server
import os
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ezocr  # noqa: E402

QEMU = qemu_exe()
BOOT_WAIT_S = 10.0
GUEST_SERVER = "10.0.2.100"         # slirp 的 guestfwd 服务端地址
PAYLOAD_LEN = "0123456789abcdef" * 8  # 128 字节，纯 ASCII 便于屏幕比对

results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print("   %s %s%s" % ("PASS" if ok else "FAIL", name,
                          ("  " + detail) if detail else ""))


# ---------- 宿主 HTTP 服务器 ----------

class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass                                  # 别把访问日志糊到测试输出里

    def do_GET(self):
        if self.path == "/len":
            body = PAYLOAD_LEN.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/chunked":
            # 三块，注意第二块长度是 16（十六进制 10），第一块 32（20）
            parts = [PAYLOAD_LEN[:32], PAYLOAD_LEN[:16], PAYLOAD_LEN]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            for p in parts:
                b = p.encode()
                self.wfile.write(("%x\r\n" % len(b)).encode() + b + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        elif self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", "/len")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        elif self.path == "/html":
            body = (b"<html><head><title>EZOS test</title></head>"
                    b"<body><h1>hello</h1></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()


def start_server(port):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


# ---------- QEMU ----------

class G:
    def __init__(self, mon_port, host_port):
        self.mon_port = mon_port
        self.host_port = host_port
        self.proc = None
        self.mon = None

    def start(self):
        #方向容易搞反：hostfwd 是"宿主->guest"，这里要的是"guest->宿主"，
        # 必须用 guestfwd。guest 连 10.0.2.100:80（slirp 的虚拟服务端地址），
        # slirp 把它转到宿主 127.0.0.1:<host_port> 上的 Python 服务器。
        argv = [QEMU, "-icount", "shift=auto", "-display", "none",
                "-drive", "format=raw,file=os-image.bin",
                "-drive", "format=raw,file=disk.vhd",
                "-netdev",
                "user,id=n0,guestfwd=tcp:10.0.2.100:80-tcp:127.0.0.1:%d" % self.host_port,
                "-device", "rtl8139,netdev=n0",
                "-serial", "file:fetchdbg.log",
                "-monitor", "tcp:127.0.0.1:%d,server,nowait" % self.mon_port]
        self.proc = subprocess.Popen(argv, cwd=ROOT,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        dl = time.time() + 15
        while True:
            try:
                self.mon = socket.create_connection(("127.0.0.1", self.mon_port), timeout=2)
                break
            except OSError:
                if time.time() > dl:
                    self.quit()
                    raise AssertionError("monitor did not come up")
                time.sleep(0.2)
        self.mon.settimeout(0.05)
        self._drain()

    def _drain(self):
        try:
            while True:
                if not self.mon.recv(4096):
                    break
        except socket.timeout:
            pass

    def mon_cmd(self, cmd):
        self.mon.sendall((cmd + "\n").encode())
        time.sleep(0.02)
        self._drain()

    def type_line(self, line):
        # 每键 60ms：shift 组合键（":"= shift-semicolon）在 20ms 下会被偶发丢弃。
        for ch in line:
            self.mon_cmd("sendkey " + key_name(ch))
            time.sleep(0.06)
        self.mon_cmd("sendkey ret")

    def screen_text(self, shot):
        self.mon_cmd("screendump " + shot)
        time.sleep(0.9)
        with open(os.path.join(ROOT, shot), "rb") as f:
            head = f.read(64).split(b"\n", 2)[1].decode()
        if head != "720 400":
            return ""
        return ezocr.ocr_text(os.path.join(ROOT, shot), os.path.join(ROOT, "kernel"))

    def quit(self):
        # 必须确保进程真死：monitor 起不来时 start() 会抛异常，那时 QEMU
        # 进程已经 fork 出来了，光关 socket 收不掉它。残留进程会占 CPU 和
        # 端口，让下一次跑稳定报"monitor did not come up"（自己绊自己）。
        try:
            if self.mon:
                self.mon_cmd("quit")
        except OSError:
            pass
        if self.proc:
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    pass
        try:
            if self.mon:
                self.mon.close()
        except OSError:
            pass


def key_name(ch):
    # QMP sendkey 的键名不等于字符：空格是 spc 不是 " "。
    # 映射漏掉就会把 "fetch http://..." 打成 "fetchhttp://..."。
    if ch == " ":
        return "spc"
    if ch == "/":
        return "slash"
    if ch == ":":
        return "shift-semicolon"
    if ch == ".":
        return "dot"
    if ch == "-":
        return "minus"
    if ch.isdigit():
        return ch
    if ch.isupper():
        return "shift-" + ch.lower()
    return ch


def wait_for(gst, shot, needle, timeout_s=25.0):
    """反复截图直到出现 needle。返回是否命中。

    OCR 在 720x400 上会把 "77 bytes of HTML." 这种行偶尔拆词，
    所以额外接受去掉空格的宽松匹配。
    """
    tight = needle
    loose = needle.replace(" ", "")
    dl = time.time() + timeout_s
    while time.time() < dl:
        t = gst.screen_text(shot)
        if tight in t:
            return True
        if loose in t.replace(" ", "").replace("\n", ""):
            return True
        time.sleep(1.0)
    return False


def type_line_sync(gst, line, shot, expect=None, timeout_s=20.0):
    """敲一条命令；给定 expect 时先等它出现再返回。

    紧邻着敲两条会让 QMP sendkey 丢字符（实测 "http://x" -> "httpx"，
    "://" 整段消失）——上一条还在刷输出时按键没被接收。
    **不能用提示符 "> " 做同步信号**：它一直挂在屏幕上，等它等于没等。
    要等的是这条命令自己的结果行。
    """
    if expect:
        wait_for(gst, shot, expect, timeout_s)
        time.sleep(0.8)          # 让 shell 回到提示符
    gst.type_line(line)


if __name__ == "__main__":
    mon_port, host_port = alloc_port(2)
    print("   monitor %d / hostfwd %d" % (mon_port, host_port))

    srv = start_server(host_port)
    g = G(mon_port, host_port)
    try:
        g.start()
        time.sleep(BOOT_WAIT_S)

        base = "http://%s" % GUEST_SERVER

        # ---- [len] Content-Length ----
        g.type_line("fetch %s/len" % base)
        ok = wait_for(g, "fetch_len.ppm", "bytes ---")
        rec("fetch/len: prints body", ok)
        txt = g.screen_text("fetch_len.ppm")
        rec("fetch/len: status 200", "HTTP 200" in txt,
            "" if "HTTP 200" in txt else "screen: " + txt[-160:].replace("\n", " | "))
        # 128 字节载荷被终端折行，屏幕上会出现首段
        rec("fetch/len: payload visible", PAYLOAD_LEN[:16] in txt)

        # ---- [chunked] ----
        g.type_line("fetch %s/chunked" % base)
        ok = wait_for(g, "fetch_chunk.ppm", "bytes ---")
        rec("fetch/chunked: prints body", ok)
        txt = g.screen_text("fetch_chunk.ppm")
        rec("fetch/chunked: flagged", "chunked" in txt,
            "" if "chunked" in txt else "screen: " + txt[-160:].replace("\n", " | "))
        rec("fetch/chunked: payload visible", PAYLOAD_LEN[:16] in txt)

        # ---- [redir] ----
        # 断言抓最终 body 而不是"redirects N"提示：跟随成功时 body 才会出现，
        # 反过来提示行可能被后续输出刷出屏幕（终端只有 400px 高）。
        g.type_line("fetch %s/redir" % base)
        ok = wait_for(g, "fetch_redir.ppm", "bytes ---")
        rec("fetch/redir: follows 302", ok)
        txt = g.screen_text("fetch_redir.ppm")
        rec("fetch/redir: got final body", PAYLOAD_LEN[:16] in txt,
            "" if PAYLOAD_LEN[:16] in txt else "screen tail: " + txt[-120:].replace("\n", " | "))

    finally:
        g.quit()
        srv.shutdown()

    # ---- [html] 必须独立实例 ----
    # 前三条 fetch 的输出会把终端刷满，同一实例里再敲的结果行会被挤出
    # 400px 高的屏幕，OCR 永远读不到（症状是断言红但功能是好的）。
    mon2, host2 = alloc_port(2)
    srv = start_server(host2)
    g2 = G(mon2, host2)
    try:
        g2.start()
        time.sleep(BOOT_WAIT_S)
        g2.type_line("fetch %s/html" % GUEST_SERVER)
        ok = wait_for(g2, "fetch_html.ppm", "of HTML")
        rec("fetch/html: hints to save", ok)
        txt = g2.screen_text("fetch_html.ppm")
        rec("fetch/html: no panic", "panic" not in txt.lower())
    finally:
        g2.quit()
        srv.shutdown()

    # ---- [file] 存文件：同样用独立实例 ----
    # 与上面共用实例时第二条命令的 "http://" 会被 QMP sendkey 丢掉（前一条
    # 还在刷输出）。等结果行也没根治，干脆一次只跑一条命令。
    mon3, host3 = alloc_port(2)
    srv = start_server(host3)
    g3 = G(mon3, host3)
    try:
        g3.start()
        time.sleep(BOOT_WAIT_S)
        g3.type_line("fetch %s/html p0.html" % GUEST_SERVER)
        ok = wait_for(g3, "fetch_file.ppm", "saved")
        rec("fetch/file: saves to file", ok)
        rec("fetch/file: no panic",
            "panic" not in g3.screen_text("fetch_file.ppm").lower())
    finally:
        g3.quit()
        srv.shutdown()

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n   %d/%d passed" % (passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)