"""BIOS/QEMU: exercise both desktop session controls with real guest input."""
from pathlib import Path
import re
import tempfile
import time
from unittest.mock import patch

from test_accounts import (
    LOGIN, ROOT_PASS, ROOT_PROMPT, USER_PROMPT, boot, enroll, inputs, sign_in,
)
from test_foundation import wait_for
from tools import gen_diskimg


def key(qmp, name):
    qmp.hmc("sendkey " + name)
    time.sleep(0.18)


def state(qmp, serial):
    start = serial.size()
    key(qmp, "f12")
    output = wait_for(serial, start, lambda s: "[GW] end" in s)
    state_line = re.search(r"\[GW\] seq=\d+ n=(\d+) focus=\d+ menu=(\d+) mx=(\d+) my=(\d+)", output)
    assert state_line, output
    return tuple(map(int, state_line.groups()))


def screenshot(qmp, path):
    response = qmp.cmd("screendump", filename=str(path))
    assert "return" in response, response
    header, size, colors, pixels = path.read_bytes().split(b"\n", 3)
    assert header == b"P6" and colors == b"255"
    return (*map(int, size.split()), pixels)


def click_at(qmp, serial, x, y):
    _, _, mx, my = state(qmp, serial)
    while mx != x or my != y:
        dx, dy = max(-24, min(24, x - mx)), max(-24, min(24, y - my))
        qmp.hmc(f"mouse_move {dx} {dy}")
        mx, my = mx + dx, my + dy
        time.sleep(0.035)
    time.sleep(0.12)
    assert state(qmp, serial)[2:] == (x, y)
    qmp.hmc("mouse_button 1")
    time.sleep(0.18)
    qmp.hmc("mouse_button 0")


def exercise(qmp, serial, scratch):
    wait_for(serial, 0, lambda s: LOGIN in s)
    sign_in(qmp, serial, "root", None, ROOT_PROMPT)
    enroll(qmp, serial, "passwd", ROOT_PASS, "Password changed.", ROOT_PROMPT)
    inputs(qmp, serial, "write secret.txt desktop-root-token", "File written successfully.", ROOT_PROMPT)
    qmp.type_line("desktop")
    time.sleep(4.0)
    assert state(qmp, serial)[0] == 0
    key(qmp, "t")
    assert state(qmp, serial)[0] == 1
    qmp.type_line("cat secret.txt")
    time.sleep(0.25)
    width, height, desktop = screenshot(qmp, scratch / "desktop.ppm")

    def terminal_text(frame):
        x, y = width * 30 // 640 + 2, height * 24 // 480 + 18
        return b"".join(frame[(row * width + x) * 3:(row * width + x + 190) * 3]
                        for row in range(y, y + 68))

    # Click the real Lock button, not a debugger shortcut.
    key(qmp, "f10")
    assert state(qmp, serial)[1] == 1
    menu_y = height * 8 // 480 - 2
    button_y = menu_y + max(30, height * 50 // 480) - 10
    menu_x = width * 6 // 640
    start = serial.size()
    click_at(qmp, serial, menu_x + 20, button_y)
    wait_for(serial, start, lambda s: "DESKTOP: locked" in s)
    time.sleep(0.25)
    _, _, locked = screenshot(qmp, scratch / "locked.ppm")
    # A panel cannot leave the old desktop visible behind it.
    outside = [(4, 4), (width - 8, 8), (8, height - 8), (width - 8, height - 8)]
    sample = lambda frame, x, y: frame[(y * width + x) * 3:(y * width + x) * 3 + 3]
    assert len({sample(locked, x, y) for x, y in outside}) == 1
    assert any(sample(locked, x, y) != sample(desktop, x, y) for x, y in outside)

    start = serial.size()
    for name in ("f12", "alt-tab", "esc", "q"):
        key(qmp, name)
    key(qmp, "esc")  # discard the q from the password box
    assert "[GW]" not in serial.tail_from(start) and ROOT_PROMPT not in serial.tail_from(start)
    inputs(qmp, serial, "wrong password", "DESKTOP: authentication failed")
    time.sleep(0.9)
    inputs(qmp, serial, ROOT_PASS, "DESKTOP: unlocked")
    assert state(qmp, serial)[0] == 1  # the original terminal survived
    _, _, unlocked = screenshot(qmp, scratch / "unlocked.ppm")
    assert terminal_text(unlocked) == terminal_text(desktop)

    # Cancel an initial switch, then confirm the actual menu click.
    key(qmp, "f10")
    key(qmp, "u")
    key(qmp, "esc")
    assert state(qmp, serial)[0] == 1
    key(qmp, "f10")
    click_at(qmp, serial, menu_x + 90, button_y)
    start = serial.size()
    key(qmp, "ret")
    wait_for(serial, start, lambda s: "DESKTOP: sign-in" in s)
    inputs(qmp, serial, "user", "DESKTOP: signed in")
    assert state(qmp, serial)[0] == 0
    key(qmp, "b")  # the standard user cannot launch the shared browser state
    assert state(qmp, serial)[0] == 0
    key(qmp, "t")
    assert state(qmp, serial)[0] == 1
    _, _, fresh = screenshot(qmp, scratch / "fresh.ppm")
    assert terminal_text(fresh) != terminal_text(desktop)
    key(qmp, "up")
    _, _, history = screenshot(qmp, scratch / "history.ppm")
    assert terminal_text(fresh) == terminal_text(history)
    key(qmp, "f10")
    start = serial.size()
    key(qmp, "l")
    wait_for(serial, start, lambda s: "DESKTOP: locked" in s)
    inputs(qmp, serial, "", "DESKTOP: unlocked")
    assert state(qmp, serial)[0] == 1

    # Back to Terminal: the active identity and home directory changed.
    key(qmp, "f10")
    row = height * 34 // 480
    title = max(30, height * 50 // 480) + height * 4 // 480
    menu_h = height - (32 if height >= 768 else 28 if height >= 400 else 18) - height * 8 // 480
    while row > height * 22 // 480 and title + 12 * row > menu_h - height * 60 // 480:
        row -= 1
    start = serial.size()
    click_at(qmp, serial, menu_x + 64, menu_y + title + 9 * row + row // 2)
    wait_for(serial, start, lambda s: USER_PROMPT in s)
    inputs(qmp, serial, "cat /home/root/secret.txt", "File not found or read error.", USER_PROMPT)
    assert ROOT_PASS not in serial.snapshot()


def main():
    with tempfile.TemporaryDirectory(prefix="cerulia-desktop-accounts-") as tmp:
        scratch = Path(tmp)
        disk = scratch / "disk.vhd"
        with patch.object(gen_diskimg, "exfat_files", return_value=[]):
            gen_diskimg.gen_exfat(disk)
        boot(disk, lambda qmp, serial: exercise(qmp, serial, scratch), False)
    print("PASS desktop lock, input isolation, graphical sign-in and account switch")


if __name__ == "__main__":
    main()
