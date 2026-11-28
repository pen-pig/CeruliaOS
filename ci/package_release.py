"""Package the tested BIOS image, data disk, launcher, and license notices."""
import argparse
import hashlib
from pathlib import Path
import re
import tarfile
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "os-image.bin": "os-image.bin",
    "disk.img": "disk.vhd",
    "tools/gen_diskimg.py": "gen_diskimg.py",
    "tools/make_vhd.py": "make_vhd.py",
    "release/setup_and_run.bat": "setup_and_run.bat",
    "release/setup_and_run.sh": "setup_and_run.sh",
    "linker.ld": "linker.ld",
    "boot/boot.asm": "boot.asm",
}
VENDOR = (
    "unifont/LICENSE.txt", "unifont/README.md",
    "webjs/LICENSE.quickjs", "webjs/COPYRIGHT.musl", "webjs/README.md",
    "wuffs/LICENSE", "wuffs/LICENSE-APACHE", "wuffs/LICENSE-MIT", "wuffs/README.md",
    "tjpgdec/README.md", "tjpgdec/tjpgd.h", "tjpgdec/tjpgd.c",
)


def package(tag: str, root: Path = ROOT) -> Path:
    if not re.fullmatch(r"v[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", tag):
        raise ValueError(f"unsupported release tag: {tag!r}")
    out = root / f"EZOS-{tag}.zip"
    part = out.with_suffix(".zip.tmp")
    sources = FILES | {f"third_party/{name}": f"licenses/{name}" for name in VENDOR}
    for source in sources:
        if not (root / source).is_file():
            raise FileNotFoundError(root / source)
    with tarfile.open(root / "third_party/bearssl/bearssl-0.6.tar.gz", "r:gz") as tar:
        with tar.extractfile("bearssl-0.6/LICENSE.txt") as f:
            bearssl_license = f.read()

    with ZipFile(part, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for source, dest in sources.items():
            archive.write(root / source, f"dist/{dest}")
        archive.writestr("dist/licenses/bearssl/LICENSE.txt", bearssl_license)
    with ZipFile(part) as archive:
        if (bad := archive.testzip()) is not None:
            raise RuntimeError(f"broken release archive entry: {bad}")
    with part.open("rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    part.replace(out)
    out.with_suffix(".zip.sha256").write_text(f"{digest}  {out.name}\n", encoding="ascii")
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("tag")
    print(package(p.parse_args().tag))
