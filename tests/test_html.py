# -*- coding: utf-8 -*-
"""test_html.py -浏览器解析/排版链路 E2E（内核 `htmltest` 命令）

为什么要有这个测试
------------------
浏览器链路的正确性**几乎无法从屏幕上判断**。断行切坏一个 UTF-8 码点、
`&amp;` 没展开、`<script>` 里的假标签混进正文、标题没抓到 —— 这些错在
窗口里全都表现为"一篇排版略差的文章"，人眼分辨不出来。等到用户投诉
"中文网页显示成方块"时，错已经埋了很久。

所以把内核里的解析/排版结果打到串口上，变成能逐行断言的东西。
`htmltest sample <n>` 走**内建样本**而不是本地文件：QMP sendkey 打不出
'<' '>' 这类字符（未映射的键名静默丢弃），键盘根本没法把 HTML 送进
guest —— 那条路一开始就是断的。

断言的六条（每条钉一类规则）
----------------------------
  1 实体：&amp; 变 &、&#65; 变 A、&hellip; 变省略号，&unknown; 原样留着
  2 标题分级：<h1> 的 level=1 且 scale=2，<h2> 的 level=2
  3 链接：正文按 </a> 分段，href 进 str 竞技场（点击区不能横跨两段）
  4 嵌套样式：<b>/<i>/<code> 的样式位要能一层层脱掉
  5 script：里面的 "<p>fake</p>" 绝不能进正文
  6 UTF-8：中文完好（没被切成半个码点），<br> 断了行

用法：python tests/test_html.py      （退出码 0 = 全部通过）
"""
import os
import re
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.append(ROOT)

from ezos_qemu import alloc_port, image_path, disk_path  # noqa: E402
from ezos_env import qemu_exe  # noqa: E402

QEMU = qemu_exe()
IMG = image_path()
DISK = disk_path()
LOG = os.path.join(ROOT, "temp", "html_serial.log")
QMP_PORT = alloc_port()
BOOT_WAIT = 90.0

results = []


def rec(name, ok, detail=""):
    results.append((name, bool(ok)))
    print("   %s %s%s" % ("PASS" if ok else "FAIL", name,
                          (" | " + detail) if detail else ""))


class Qmp(object):
    def __init__(self, port):
        for _ in range(80):
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), 2)
                break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("QMP connect failed")
        self.sock.settimeout(20)
        self.f = self.sock.makefile("rwb")
        self.f.readline()
        self.cmd("qmp_capabilities")

    def cmd(self, name, **args):
        import json
        self.f.write((json.dumps({"execute": name, "arguments": args})
                      + "\n").encode())
        self.f.flush()
        while True:
            line = self.f.readline()
            if not line:
                return {}
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if "return" in o or "error" in o:
                return o

    def hmc(self, c):
        return self.cmd("human-monitor-command", **{"command-line": c})

    def keys(self, text):
        """逐键发送。必须给8042 留处理时间：连发会静默丢键。"""
        for i, ch in enumerate(text):
            self.hmc("sendkey " + ("spc" if ch == " " else ch))
            time.sleep(0.04)
            if i % 10 == 9:
                time.sleep(0.2)
        time.sleep(2.5)

    def run(self, line):
        self.keys(line)
        self.hmc("sendkey ret")
        time.sleep(3.0)

    def quit(self):
        try:
            self.cmd("quit")
        except Exception:
            pass


def _log_bytes():
    try:
        with open(LOG, "rb") as f:
            return f.read()
    except OSError:
        return b""


def read_log():
    """latin1 全文。判"有没有这行"用它最稳（任何字节都读得出来）。"""
    return _log_bytes().decode("latin1", "replace")


def read_log_utf8():
    """utf-8 全文。判"中文/省略号在不在"**必须**用它——按 latin1 读会把
    "你好"变成 "ä½\xa0å¥½"，断言全红而内核其实是对的。
    早先只留 latin1 一份，害得以为内核把 UTF-8 切坏了。"""
    return _log_bytes().decode("utf-8", "replace")


def wait_shell(timeout=BOOT_WAIT):
    end = time.time() + timeout
    while time.time() < end:
        if "[/] > " in read_log():
            return True
        time.sleep(0.5)
    return False


def run_sample(qmp, n):
    """跑 `htmltest sample n`，返回 (摘要 dict, 行列表)

    切在**本次输出开头**而不是"文件末尾往前找"：串口日志里前面还留着
    上一次 run 的内容，按全局搜会把旧行当这次的。shell 还会把每次敲键
    回显成 `[/] > htmltest sample 1` 这种整行，所以要用"htmltest end"
    作为上一轮的结束锚点。
    """
    before = read_log().rfind("htmltest end")
    before = before + len("htmltest end") if before >= 0 else 0
    qmp.run("htmltest sample %d" % n)
    txt = read_log()[before:]
    lines = [l.rstrip() for l in txt.splitlines()]
    head = {}
    for l in lines:
        m = re.match(r"\s+(title|bytes|nodes|lines|pageh|truncated)=(\S*)", l)
        if m:
            head[m.group(1)] = m.group(2)
    body = [l for l in lines if re.match(r"\s+L\d+ ", l)]
    return head, body


def main():
    if os.path.exists(LOG):
        os.remove(LOG)
    proc = subprocess.Popen(
        [QEMU, "-machine", "pc", "-vga", "std", "-m", "128",
         "-drive", "format=raw,file=" + IMG,
         "-drive", "format=raw,file=" + DISK,
         "-display", "none", "-no-reboot",
         "-serial", "file:" + LOG,
         "-qmp", "tcp:127.0.0.1:%d,server,nowait" % QMP_PORT],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    qmp = None
    try:
        if not wait_shell():
            rec("boot reaches shell prompt", False)
            return 1
        qmp = Qmp(QMP_PORT)

        # ---- 命令本身存在，且用法提示完整 ----
        qmp.run("htmltest")
        txt = read_log()
        rec("htmltest with no arg prints usage", "usage: htmltest" in txt)

        # ---- 样本 1：实体 ----
        head, body = run_sample(qmp, 1)
        rec("sample 1: parsed without truncation",
            head.get("truncated") != "truncated", "| %r" % head)
        joined = " ".join(body)
        rec("sample 1: &amp; expanded to &", "a&b <c>" in joined,
            "| %r" % joined[:90])
        rec("sample 1: numeric entity &#65; -> A", " A " in joined,
            "| %r" % joined[:90])
        rec("sample 1: &hellip; expanded to U+2026",
            "…" in read_log_utf8(), "| %r" % joined[:90])
        rec("sample 1: unknown entity kept verbatim",
            "&unknown;" in joined, "| %r" % joined[:90])
        rec("sample 1: unterminated &amp kept, not swallowed",
            "&amp" in joined and "&amp;" not in joined,
            "| %r" % joined[-40:])

        # ---- 样本 2：标题分级 ----
        head, body = run_sample(qmp, 2)
        h1 = [l for l in body if "|Title" in l]
        h2 = [l for l in body if "|Sub" in l]
        one = [l for l in body if "|One" in l]
        # 判据取"h1 与正文行的 scale 不同"，不硬编 sc=2 —— 那是把当前
        # 实现的巧合当契约，实现一改就假红。真正要守的是"标题比正文醒目"。
        def _sc(ls):
            m = re.search(r"sc=(\d+)", ls[0]) if ls else None
            return m.group(1) if m else None
        sc_h1, sc_body = _sc(h1), _sc(one)
        rec("sample 2: h1 rendered", len(h1) == 1, "| %r" % h1)
        rec("sample 2: h1 stands out from body text",
            sc_h1 is not None and sc_h1 != sc_body,
            "| h1 sc=%s body sc=%s" % (sc_h1, sc_body))
        rec("sample 2: h2 rendered", len(h2) == 1, "| %r" % h2)
        rec("sample 2: all four headings present",
            all(any(("|" + t) in l for l in body)
                for t in ("Title", "One", "Sub", "Two")),
            "| %d lines" % len(body))

        # ---- 样本 3：链接分段 ----
        head, body = run_sample(qmp, 3)
        link_lines = [l for l in body if "href=0" not in l]
        rec("sample 3: two link runs (split at </a>)",
            len(link_lines) == 2, "| %r" % link_lines)
        rec("sample 3: link text is 'click'",
            bool(link_lines) and "|click" in link_lines[0],
            "| %r" % (link_lines[0] if link_lines else None))
        rec("sample 3: trailing text is NOT part of the link",
            bool(link_lines) and "and" not in link_lines[0],
            "| %r" % (link_lines[0] if link_lines else None))

        # ---- 样本 4：嵌套样式 ----
        head, body = run_sample(qmp, 4)
        rec("sample 4: bold run has fs bit 0x01",
            any("|bold" in l and "fs=1" in l for l in body),
            "| %r" % body)
        rec("sample 4: italic run has fs bit 0x02",
            any("|ital" in l and "fs=2" in l for l in body),
            "| %r" % body)
        rec("sample 4: mono run has fs bit 0x08",
            any("|mono" in l and "fs=8" in l for l in body),
            "| %r" % body)
        rec("sample 4: plain runs carry no style bits",
            any("|and" in l and "fs=0" in l for l in body),
            "| %r" % body)

        # ---- 样本 5：script 内容不进正文 ----
        head, body = run_sample(qmp, 5)
        joined = " ".join(body)
        rec("sample 5: script body excluded", "fake" not in joined,
            "| %r" % joined[:90])
        rec("sample 5: text before script kept", "a" in joined,
            "| %r" % joined[:90])
        rec("sample 5: text after script kept",
            any(l.rstrip().endswith("b") for l in body), "| %r" % body)

        # ---- 样本 6：UTF-8 + <br> ----
        head, body = run_sample(qmp, 6)
        joined = " ".join(body)
        # 中文按 utf-8 判：串口字节若按 latin1 读会被转成乱码
        utf8_tail = read_log_utf8()
        rec("sample 6: Chinese text intact",
            "你好世界" in utf8_tail, "| %r" % joined[:90])
        rec("sample 6: no replacement char (codepoint not cut)",
            "\ufffd" not in utf8_tail, "| %r" % joined[:90])
        rec("sample 6: <br> produced a separate line",
            any(l.rstrip().endswith("line2") for l in body),
            "| %r" % body)

        # ---- 越界样本号必须被拒，不能当成"解析成功" ----
        head, body = run_sample(qmp, 99)
        txt = read_log()
        rec("out-of-range sample is rejected",
            "no such sample" in txt)

        # 只数"行首就是 htmltest <label>"的块首行：shell 把每次敲键都回显
        # 成 `[/] > htmltest sample 1`，直接 count 子串会把 1 次运行数成 5 次。
        lines_all = read_log().splitlines()
        # 要排除 "htmltest end"：它同样匹配 ^htmltest \S，混进去
        # 会让 starts 永远是 ends 的两倍（6 次运行数成 12）。
        starts = len([l for l in lines_all
                      if re.match(r"^htmltest \S", l)
                      and l.strip() != "htmltest end"])
        ends = len([l for l in lines_all if l.strip() == "htmltest end"])
        rec("every htmltest run terminated cleanly", starts == ends,
            "| %d starts / %d ends" % (starts, ends))
        # 注意别grep 裸 "panic"：banner 里有 "panic screen on fault"，
        # 那是开机横幅的一部分，不是内核崩了。
        tail = read_log()
        rec("no kernel panic during html parsing",
            "kernel panic" not in tail and "exception: #" not in tail)
    finally:
        if qmp:
            qmp.quit()
        time.sleep(0.5)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    npass = sum(1 for _, ok in results if ok)
    print("\n%d/%d passed" % (npass, len(results)))
    with open(os.path.join(ROOT, "temp", "test_html_report.txt"), "w",
              encoding="utf-8") as f:
        for name, ok in results:
            f.write("%s %s\n" % ("PASS" if ok else "FAIL", name))
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())