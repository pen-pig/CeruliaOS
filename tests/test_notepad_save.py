"""Click the real Notepad menus; check the saved bytes after a cold boot."""
from pathlib import Path
import re
import tempfile
import time
from unittest.mock import patch

from test_accounts import LOGIN, ROOT_PROMPT, USER_PROMPT, boot, inputs
from ref_exfat import Exfat
from test_desktop_accounts import click_at, key, screenshot, state
from test_foundation import wait_for
from tools import gen_diskimg


def launch(qmp, serial, work, existing=False, app=1):
    wait_for(serial, 0, lambda s: LOGIN in s)
    if existing:
        inputs(qmp, serial, "root", ROOT_PROMPT)
        inputs(qmp, serial, "cd /", "[root:/] > ")
    else:
        inputs(qmp, serial, "user", USER_PROMPT)
    qmp.type_line("desktop")
    time.sleep(4)
    key(qmp, "f10")
    width, height, _ = screenshot(qmp, work / "desktop.ppm")
    row = height * 34 // 480
    title = max(30, height * 50 // 480) + height * 4 // 480
    menu_h = height - (32 if height >= 768 else 28 if height >= 400 else 18) - height * 8 // 480
    while row > height * 22 // 480 and title + 12 * row > menu_h - height * 60 // 480:
        row -= 1
    click_at(qmp, serial, width * 6 // 640 + 64, height * 8 // 480 - 2 + title + app * row + row // 2)
    start = serial.size()
    state(qmp, serial)
    m = re.search(rf" app={app} x=(\d+) y=(\d+)", serial.tail_from(start))
    assert m, serial.tail_from(start)
    return tuple(map(int, m.groups()))


def edit(qmp, serial, work, existing=False):
    x, y = launch(qmp, serial, work, existing)
    for k in (("x",) if existing else ("a", "b", "left", "ret", "backspace", "ret", "x")):
        key(qmp, k)
    click_at(qmp, serial, x + 9, y + 18)
    click_at(qmp, serial, x + 15, y + 13 + 14 + 2*16 + 6)
    time.sleep(0.3)


def readback(qmp, serial):
    wait_for(serial, 0, lambda s: LOGIN in s)
    inputs(qmp, serial, "user", USER_PROMPT)
    inputs(qmp, serial, "cat NOTE.TXT", "\na\r\nxb\r\n" + USER_PROMPT)


def office(qmp, serial, work):
    # In the fixture the first directory entry is the long document. Use the
    # actual file preview, which is capped at 2047 bytes, then edit its tail.
    launch(qmp, serial, work, existing=True, app=0)
    key(qmp, "ret")
    key(qmp, "e")
    start = serial.size()
    state(qmp, serial)
    m = re.search(r" app=1 x=(\d+) y=(\d+)", serial.tail_from(start))
    assert m, serial.tail_from(start)
    x, y = map(int, m.groups())

    def menu(edit_menu, item):
        dx = 41 if edit_menu else 9
        click_at(qmp, serial, x + dx, y + 18)
        click_at(qmp, serial, x + dx, y + 13 + 14 + item*16 + 6)

    menu(True, 1)  # end of document
    key(qmp, "backspace")  # one multibyte character
    key(qmp, "f2")
    qmp.type_line("6587")  # 文
    time.sleep(0.4)
    menu(False, 3)
    qmp.type_line("taken.txt")  # refuse to overwrite; keep unsaved edit
    time.sleep(0.4)
    menu(False, 3)
    qmp.type_line("edited.txt")
    time.sleep(0.4)
    menu(False, 0)
    qmp.type_line("memo.txt")
    time.sleep(0.4)
    key(qmp, "m")
    key(qmp, "f3")
    menu(False, 1)
    qmp.type_line("edited.txt")
    time.sleep(0.4)
    key(qmp, "x")
    key(qmp, "f3")
    time.sleep(0.4)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="cerulia-notepad-") as tmp:
        work = Path(tmp)
        disk = work / "data.vhd"
        gen_diskimg.gen_exfat(disk)
        boot(disk, lambda q, s: edit(q, s, work), False)
        boot(disk, readback, False)
        for raw, expected in ((b"one\r\ntwo", b"xone\r\ntwo"), (b"Z" * 41, b"x" + b"Z" * 41),
                              (b"L" * 32768, b"L" * 32768),
                              (b"bad\x80", b"bad\x80"), (b"L" * 32769, b"L" * 32769)):
            with patch.object(gen_diskimg, "exfat_files", return_value=[("NOTE.TXT", raw)]):
                gen_diskimg.gen_exfat(disk)
            boot(disk, lambda q, s: edit(q, s, work, existing=True), False)
            actual = Exfat(disk).read_path("/NOTE.TXT")
            assert actual == expected, (len(raw), actual[:40] if actual else actual)
        raw = (b"A" * 140 + b"\r\nline\tend\n") * 32 + "中文字".encode()
        fixtures = [("REPORT.TXT", raw), ("TAKEN.TXT", b"keep me")]
        with patch.object(gen_diskimg, "exfat_files", return_value=fixtures):
            gen_diskimg.gen_exfat(disk)
        boot(disk, lambda q, s: office(q, s, work), False)
        fs = Exfat(disk)
        assert fs.read_path("/REPORT.TXT") == raw
        assert fs.read_path("/TAKEN.TXT") == b"keep me"
        assert fs.read_path("/edited.txt") == b"x" + raw[:-3] + "文".encode()
        assert fs.read_path("/memo.txt") == b"m"
    print("PASS Notepad cold boot, limits, UTF-8, full-file editing, named open/new/save as")
