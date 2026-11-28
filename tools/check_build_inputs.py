"""Preflight the checked-in Ninja link graph before building either OS image.

Only static paths in this project's build statements are supported. Generated
outputs are followed through their producing edges, not required on disk.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import sys
from contextlib import ExitStack


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "os-image.bin"
KERNEL = "kernel_raw.bin"
RELEASE_INPUTS = (
    ".github/workflows/main.yml", ".gitattributes", ".githooks/pre-commit", ".githooks/pre-push",
    "tools/check_build_inputs.py", "tests/test_build_inputs.py",
    "ci/build.sh", "ci/smoke.sh", "ci/smoke_guest.py", "tests/test_image.py",
    "tools/check_stack.py", "tools/check_cpu.py", "tools/check_vmx.py", "tools/host_probe.py",
    "ci/package_release.py", "release/setup_and_run.bat", "release/setup_and_run.sh",
    "tools/gen_diskimg.py", "tools/make_vhd.py",
    "third_party/unifont/LICENSE.txt", "third_party/unifont/README.md",
    "third_party/wuffs/LICENSE", "third_party/wuffs/LICENSE-APACHE", "third_party/wuffs/LICENSE-MIT",
    "third_party/wuffs/README.md", "third_party/tjpgdec/README.md",
    "third_party/tjpgdec/tjpgd.h", "third_party/tjpgdec/tjpgd.c",
    "third_party/webjs/LICENSE.quickjs", "third_party/webjs/COPYRIGHT.musl",
    "third_party/webjs/README.md",
)
EXTRA_INPUTS = ("linker.ld", "rust/ezos_rs/Cargo.lock", "rust/ezos_rs/rust-toolchain.toml")
QUOTED_INCLUDE = re.compile(r'^\s*#\s*include\s*"([^"\n]+)"', re.MULTILINE)
ASM_INPUT = re.compile(r'^[ \t]*(%include|incbin)\s+"([^"\n]+)"', re.MULTILINE | re.IGNORECASE)
RUST_MOD = re.compile(
    r'^[ \t]*(?:#\s*\[\s*path\s*=\s*"([^"\n]+)"\s*\][ \t]*\n[ \t]*)?'
    r'(?:pub(?:\([^)\n]+\))?[ \t]+)?mod[ \t]+(\w+)[ \t]*;',
    re.MULTILINE,
)
RUST_LITERAL_INCLUDE = re.compile(r'\binclude(?:_str|_bytes)?!\s*\(\s*"([^"\n]+)"')


class DiskInputs:
    label = "working tree"

    def __init__(self, root: Path):
        self.root = root

    def exists(self, name: str) -> bool:
        return (self.root / name).is_file()

    def read(self, name: str) -> str:
        return (self.root / name).read_text(encoding="utf-8")


class GitInputs:
    """Read Git blobs, not the on-disk files that happen to have the same names."""

    def __init__(self, root: Path, commit: str | None):
        self.root = root
        self.blobs: dict[str, bytes] = {}
        if commit is None:
            self.label = "Git index"
            listing = self.git("ls-files", "--stage", "-z")
        else:
            tree = self.git("rev-parse", "--verify", "--end-of-options", f"{commit}^{{tree}}")
            self.label = f"commit {commit}"
            listing = self.git("ls-tree", "-rz", tree.decode("ascii").strip())
        for record in listing.split(b"\0"):
            if not record:
                continue
            info, raw_name = record.split(b"\t", 1)
            name = raw_name.decode("utf-8")
            if commit is None:
                mode, oid, stage = info.split()
                if stage != b"0":
                    raise ValueError(f"unmerged index path: {name}")
            else:
                mode, _, oid = info.split()
            if mode in (b"100644", b"100755"):
                self.blobs[name] = oid
        self.batch = None

    def git(self, *args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=self.root)

    def exists(self, name: str) -> bool:
        return name in self.blobs

    def read(self, name: str) -> str:
        oid = self.blobs.get(name)
        if oid is None:
            raise FileNotFoundError(f"{name} not in {self.label}")
        if self.batch is None:
            self.batch = subprocess.Popen(
                ["git", "cat-file", "--batch"], cwd=self.root,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            )
        assert self.batch.stdin is not None and self.batch.stdout is not None
        self.batch.stdin.write(oid + b"\n")
        self.batch.stdin.flush()
        header = self.batch.stdout.readline().split()
        if len(header) != 3 or header[:2] != [oid, b"blob"]:
            raise ValueError(f"Git could not read blob for {name}")
        size = int(header[2])
        data = self.batch.stdout.read(size)
        if len(data) != size or self.batch.stdout.read(1) != b"\n":
            raise ValueError(f"truncated Git blob for {name}")
        return data.decode("utf-8").replace("\r\n", "\n")

    def close(self) -> None:
        if self.batch is not None:
            self.batch.stdin.close()
            self.batch.stdout.close()
            self.batch.wait()


@dataclass(frozen=True)
class Edge:
    rule: str
    inputs: tuple[str, ...]


def path(name: str) -> str:
    p = PurePosixPath(posixpath.normpath(name))
    if p.is_absolute() or p.parts[:1] == ("..",) or "$" in name or "\\" in name:
        raise ValueError(f"unsupported build path {name!r}")
    return p.as_posix()


def graph(text: str) -> dict[str, Edge]:
    outputs: dict[str, Edge] = {}
    for line in re.sub(r"\$\n[ \t]*", "", text).splitlines():
        if not line.startswith("build "):
            continue
        left, right = line[6:].split(":", 1)
        rule, *names = right.split()
        edge = Edge(rule, tuple(path(n) for n in names if n not in ("|", "||", "|@")))
        for name in left.split():
            if name == "|":
                continue
            out = path(name)
            if out in outputs:
                raise ValueError(f"duplicate producer for {out}")
            outputs[out] = edge
    return outputs


def kernel_sources(edges: dict[str, Edge]) -> list[str]:
    sources = []
    for name in edges[KERNEL].inputs:
        edge = edges.get(name)
        if edge and edge.rule == "cc":
            if not edge.inputs or not edge.inputs[0].endswith(".c") or not edge.inputs[0].startswith("kernel/"):
                raise ValueError(f"kernel C edge {name} has no kernel source")
            sources.append(edge.inputs[0])
    if not sources:
        raise ValueError(f"no C source inputs in {KERNEL}")
    return sources


def missing_inputs(files: DiskInputs | GitInputs, edges: dict[str, Edge], target: str,
                   extra=()) -> dict[str, str]:
    missing: dict[str, str] = {}
    leaves: set[str] = set()
    visited: set[str] = set()
    if target not in edges:
        raise ValueError(f"no producer for {target}")

    def visit(name: str, parent: str) -> None:
        if name in visited:
            return
        visited.add(name)
        if name in edges:
            for item in edges[name].inputs:
                visit(item, name)
        elif files.exists(name):
            leaves.add(name)
        else:
            missing.setdefault(name, parent)

    visit(target, "build target")
    for name in extra:
        visit(name, "CI/release input")

    checked: set[str] = set()

    def require(names: tuple[str, ...], parent: str, *, recurse=False) -> None:
        candidate = next((name for name in names if name in edges or files.exists(name)), None)
        if candidate is None:
            missing.setdefault(names[0], parent)
        elif recurse and candidate not in edges:
            inspect(candidate)

    def inspect(source: str) -> None:
        if source in checked:
            return
        checked.add(source)

        src = PurePosixPath(source)
        directory = src.parent
        if source.startswith(("kernel/", "user/")) and src.suffix in (".c", ".h", ".inc"):
            search_root = source.split("/", 1)[0]
            for raw in QUOTED_INCLUDE.findall(files.read(source)):
                # QuickJS headers live inside the source archive checked by the Ninja graph.
                if source == "user/webjs.c" and raw == "quickjs.h":
                    continue
                require((path((directory / raw).as_posix()), path(f"{search_root}/{raw}")),
                        source, recurse=True)
        if source.startswith(("boot/", "kernel/", "user/", "uefi/")) and src.suffix in (".asm", ".inc"):
            for op, raw in ASM_INPUT.findall(files.read(source)):
                require((path(raw),), source, recurse=op.lower() == "%include")
        elif source.startswith("rust/") and src.suffix == ".rs":
            content = files.read(source)
            module_dir = directory if src.name in ("lib.rs", "main.rs", "mod.rs") else src.with_suffix("")
            for override, name in RUST_MOD.findall(content):
                choices = (directory / override,) if override else (
                    module_dir / f"{name}.rs", module_dir / name / "mod.rs",
                )
                require(tuple(path(p.as_posix()) for p in choices), source, recurse=True)
            for raw in RUST_LITERAL_INCLUDE.findall(content):
                require((path((directory / raw).as_posix()),), source)

    for name in sorted(leaves):
        inspect(name)
    return dict(sorted(missing.items()))


def verify(args, commit=None) -> int:
    with ExitStack() as stack:
        if args.tracked or commit:
            files = GitInputs(args.root, commit)
            stack.callback(files.close)
        else:
            files = DiskInputs(args.root)
        edges = graph(files.read("build.ninja"))
        extra = EXTRA_INPUTS + (RELEASE_INPUTS if args.release else ())
        missing = missing_inputs(files, edges, args.target, extra)
        if missing:
            for name, parent in missing.items():
                origin = parent if files.exists(parent) else "build.ninja"
                print(f"::error file={origin}::missing {name} in {files.label} (required by {parent})",
                      file=sys.stderr)
            print(f"{len(missing)} missing build inputs. Add the required source files to the same commit.",
                  file=sys.stderr)
            return 1
        if args.kernel_sources:
            print("\n".join(kernel_sources(edges)))
        elif args.kernel_link_inputs:
            print("\n".join(n for n in edges[KERNEL].inputs if n.endswith((".o", ".a"))))
        else:
            print(f"Build inputs complete: {args.target} ({files.label})")
        return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--target", default=IMAGE)
    p.add_argument("--release", action="store_true")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--tracked", action="store_true", help="check the exact staged Git snapshot")
    mode.add_argument("--commit", metavar="REF", help="check the exact tree of a commit or tag")
    mode.add_argument("--pre-push", action="store_true", help="check each outgoing ref sent on stdin by Git")
    mode.add_argument("--kernel-sources", action="store_true", help="emit the kernel C sources on stdout")
    mode.add_argument("--kernel-link-inputs", action="store_true", help="emit ordered kernel objects and archives")
    args = p.parse_args(argv)
    try:
        if args.pre_push:
            # Ignore deletions; verify the actual ref tip rather than HEAD or worktree.
            tips = set()
            for line in sys.stdin:
                _, oid, _, _ = line.split()
                if oid.strip("0"):
                    tips.add(oid)
            return max((verify(args, tip) for tip in sorted(tips)), default=0)
        return verify(args, args.commit)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"::error file=build.ninja::build preflight: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
