"""Boot a private guest and compare its Unicode framebuffer to upstream glyphs."""
import gzip
import tempfile
import time
from pathlib import Path

from test_accounts import LOGIN, ROOT, ROOT_PROMPT, boot, gen_diskimg, inputs, wait_for
from test_desktop_accounts import screenshot


def glyph_check(get, glyphs, ch, x, y, fg):
    mask = glyphs[ord(ch)]
    stride = len(mask) // 16
    hits = 0
    for row in range(16):
        for col in range(stride * 8):
            on = mask[row * stride + col // 8] & (0x80 >> (col % 8))
            want = fg if on else (0, 0, 0)
            if all(abs(p - q) <= 8 for p, q in zip(get(x + col, y + row), want)):
                hits += 1
    assert hits == 128 * stride, (ch, hits, 128 * stride)
    print(f"PASS U+{ord(ch):04X}: {hits}/{128 * stride} pixels, {stride * 8}px advance")


def main():
    source = ROOT / "third_party/unifont/unifont-17.0.04.hex.gz"
    with gzip.open(source, "rt", encoding="ascii") as f:
        glyphs = {int(k, 16): bytes.fromhex(v) for k, v in (line.split(":") for line in f)}
    with tempfile.TemporaryDirectory(prefix="cerulia-cjk-") as tmp:
        disk = Path(tmp) / "data.vhd"
        gen_diskimg.gen_exfat(disk)

        def exercise(q, serial):
            wait_for(serial, 0, lambda s: LOGIN in s)
            inputs(q, serial, "root", ROOT_PROMPT)
            q.type_line("cjk")
            # Store the frame outside the temporary disk so it can be inspected.
            shot = ROOT / "temp" / "cjk-unicode.ppm"
            for _ in range(20):
                time.sleep(.25)
                width, height, rgb = screenshot(q, shot)
                get = lambda x, y: rgb[(y * width + x) * 3:(y * width + x) * 3 + 3]
                if height >= 176 and any(get(64 + c, 64 + r)[1] > 200 for r in range(16) for c in range(16)):
                    break
            else:
                raise AssertionError("Chinese graphics demo never became visible")
            glyph_check(get, glyphs, "中", 64, 64, (85, 255, 85))
            glyph_check(get, glyphs, "蔚", 64, 96, (255, 255, 255))
            glyph_check(get, glyphs, "龘", 64, 160, (255, 255, 255))
            glyph_check(get, glyphs, "　", 80, 160, (255, 255, 255))
            glyph_check(get, glyphs, "㐂", 96, 160, (255, 255, 255))
            glyph_check(get, glyphs, "é", 144, 160, (255, 255, 255))
            glyph_check(get, glyphs, "，", 152, 160, (255, 255, 255))
            glyph_check(get, glyphs, "繁", 168, 160, (255, 255, 255))
            assert any(get(x, y)[1] >= 245 for y in range(128, 136) for x in range(64, 156))
            start = serial.size()
            q.hmc("sendkey a")
            wait_for(serial, start, lambda s: "cjk: demo done" in s and ROOT_PROMPT in s)
            print("PASS ASCII and return to the shell")

        boot(disk, exercise, False)


if __name__ == "__main__":
    main()
