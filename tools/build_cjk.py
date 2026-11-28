"""Pack a pinned GNU Unifont subset offline as sorted 35-byte records.

Each record has a LE BMP codepoint, advance in pixels, and 16 rows of
two bytes. 8-pixel glyphs occupy the first byte; the other is padding.
"""
import gzip
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "third_party" / "unifont" / "unifont-17.0.04.hex.gz"
SHA256 = "e808096f98ff04294f0d49452cad51964e2930b7be046ac8e1efadc46b2bab0f"
RANGES = (
    (0x00A0, 0x024F),  # Latin accents
    (0x2000, 0x22FF),  # punctuation, currency, arrows, mathematical symbols
    (0x2E80, 0x33FF),  # radicals, CJK punctuation, kana, bopomofo
    (0x3400, 0x4DBF),  # Han Extension A
    (0x4E00, 0x9FFF),  # Unified Ideographs (simplified and traditional)
    (0xF900, 0xFAFF),  # compatibility ideographs
    (0xFE10, 0xFE6F), (0xFF00, 0xFFEF),
)


def build(out):
    data = SOURCE.read_bytes()
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError(f"Unexpected checksum for {SOURCE}")
    glyphs = {}
    for line in gzip.decompress(data).decode("ascii").splitlines():
        key, raw = line.split(":")
        cp = int(key, 16)
        if not any(lo <= cp <= hi for lo, hi in RANGES):
            continue
        bitmap = bytes.fromhex(raw)
        if cp in glyphs or len(bitmap) not in (16, 32):
            raise ValueError(f"Invalid Unifont record U+{cp:04X}")
        width = 8 if len(bitmap) == 16 else 16
        if width == 8:
            bitmap = b"".join(bytes((row, 0)) for row in bitmap)
        glyphs[cp] = bytes((width,)) + bitmap
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"".join(cp.to_bytes(2, "little") + glyphs[cp] for cp in sorted(glyphs)))
    print(f"{len(glyphs)} glyphs packed into {out} ({out.stat().st_size} bytes)")


def build_gbk(out):
    """Dense 126 x 190 GBK table. Values of zero denote unassigned pairs."""
    data = bytearray()
    for lead in range(0x81, 0xFF):
        for trail in (*range(0x40, 0x7F), *range(0x80, 0xFF)):
            try:
                cp = ord(bytes((lead, trail)).decode("gbk"))
            except UnicodeDecodeError:
                cp = 0
            data.extend(cp.to_bytes(2, "little"))
    out.write_bytes(data)


if __name__ == "__main__":
    build(Path(sys.argv[1]))
    build_gbk(Path(sys.argv[2]))
