"""Test source closure independently of a populated build directory."""
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.check_build_inputs import DiskInputs, EXTRA_INPUTS, graph, kernel_sources, missing_inputs

CHECKER = Path(__file__).resolve().parents[1] / "tools" / "check_build_inputs.py"


class TestBuildInputs(unittest.TestCase):
    ninja = """
build demo.bin: image kernel_raw.bin boot/boot.bin
build kernel_raw.bin: link kernel/main.o $
    | linker.ld
build kernel/main.o: cc kernel/main.c
build boot/boot.bin: asm boot/boot.asm | boot/layout.inc
build opt: run unrelated.txt
"""

    def test_source_closure(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {
                "kernel/main.c": '#include "include/first.h"\n',
                "kernel/include/first.h": '#include "../other.h"\n',
                "boot/boot.asm": "",
            }
            for name, body in files.items():
                p = root / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body)
            edges = graph(self.ninja)
            self.assertEqual(kernel_sources(edges), ["kernel/main.c"])
            self.assertEqual(missing_inputs(DiskInputs(root), edges, "demo.bin"), {
                "boot/layout.inc": "boot/boot.bin",
                "kernel/other.h": "kernel/include/first.h",
                "linker.ld": "kernel_raw.bin",
            })
            for name in ("boot/layout.inc", "kernel/other.h", "linker.ld"):
                (root / name).touch()
            self.assertEqual(missing_inputs(DiskInputs(root), edges, "demo.bin"), {})
            self.assertFalse((root / "kernel/main.o").exists())

    def test_refuses_ambiguous_or_escaping_graph(self):
        with self.assertRaises(ValueError):
            graph("build a: run ../outside")
        with self.assertRaises(ValueError):
            graph("build a: run b\nbuild a: run c")

    @unittest.skipUnless(shutil.which("git"), "Git required for snapshot tests")
    def test_index_and_push_use_git_blobs(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args):
                cmd = ["git", "-c", "core.hooksPath=.git/no-hooks", "-c", "core.autocrlf=false",
                       "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", "user.name=Build test",
                       "-c", "user.email=test@example.invalid", *args]
                return subprocess.check_output(cmd, cwd=root, text=True).strip()

            def write(name, body=""):
                p = root / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body, encoding="utf-8")

            def check(*args, stdin=None):
                return subprocess.run(
                    [sys.executable, str(CHECKER), "--root", str(root), "--target", "demo.bin", *args],
                    input=stdin, text=True, capture_output=True,
                )

            git("init", "-q")
            write("build.ninja", self.ninja.replace(
                "image kernel_raw.bin boot/boot.bin",
                "image kernel_raw.bin boot/boot.bin rust/a.o user/main.o") + """
build rust/a.o: cargo rust/ezos_rs/src/lib.rs
build user/main.o: cc_user user/main.c
""")
            write("kernel/main.c", '#include "new.h"\n')
            write("boot/boot.asm", '%include "boot/chain.inc"\n')
            write("boot/layout.inc")
            write("rust/ezos_rs/src/lib.rs", '#[path = "sub/new.rs"]\nmod first;\n')
            write("user/main.c", '#include "main.h"\n')
            for name in EXTRA_INPUTS:
                write(name)
            git("add", "--", ".")
            git("commit", "-qm", "incomplete snapshot")
            bad = git("rev-parse", "HEAD")

            # All dependencies now exist in the worktree, but are not in Git.
            missing = ("kernel/new.h", "boot/chain.inc", "rust/ezos_rs/src/sub/new.rs", "user/main.h")
            for name in missing:
                write(name)
            self.assertEqual(check().returncode, 0)
            failure = check("--tracked")
            self.assertEqual(failure.returncode, 1, failure.stderr)
            for name in missing:
                self.assertIn(name, failure.stderr)

            # Even replacing the worktree's source does not hide the staged include.
            write("kernel/main.c")
            self.assertIn("kernel/new.h", check("--tracked").stderr)
            git("add", "--", *missing)
            # The reverse: a new unstaged include must not block committing the good index.
            write("kernel/main.c", '#include "unstaged-only.h"\n')
            self.assertNotEqual(check().returncode, 0)
            success = check("--tracked")
            self.assertEqual(success.returncode, 0, success.stderr)
            git("commit", "-qm", "include all dependencies")
            good = git("rev-parse", "HEAD")
            git("tag", "-am", "test tag", "v-test")
            tag = git("rev-parse", "v-test")
            zeros = "0" * len(good)
            outgoing_bad = f"refs/heads/old {bad} refs/heads/main {zeros}\n"
            rejected = check("--pre-push", stdin=outgoing_bad)
            self.assertEqual(rejected.returncode, 1, rejected.stderr)
            self.assertIn("kernel/new.h", rejected.stderr)
            accepted = check("--pre-push", stdin=(
                f"refs/heads/new {good} refs/heads/main {bad}\n"
                f"refs/tags/v-test {tag} refs/tags/v-test {zeros}\n"
                f"(delete) {zeros} refs/heads/old {bad}\n"
            ))
            self.assertEqual(accepted.returncode, 0, accepted.stderr)


if __name__ == "__main__":
    unittest.main()
