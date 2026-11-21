"""OVMF-only Secure Boot test. Uses throwaway keys and a copy of blank NVRAM."""
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ezos_env import ovmf_x64_fd, qemu_exe  # noqa: E402
from ezos_qemu import alloc_port  # noqa: E402
from test_boot_compat import boot, wait_for  # noqa: E402
from test_nvme import Qmp, SerialReader  # noqa: E402
from tools.make_usb_image import ESP_LBA, SECTOR  # noqa: E402


def tool(env, paths):
    override = os.environ.get(env)
    if override:
        return Path(override)
    for path in paths:
        if path and Path(path).is_file():
            return Path(path)
    raise SystemExit(f"Missing {env}; see uefi/README.md")


def run(*args):
    subprocess.run(args, check=True, cwd=ROOT)


def provision_test_vars(blank, dest, certificate):
    """Serialize test PK/db into an empty OVMF authenticated variable store.

    Refuse nonblank input. The only output is the VM-local copy, never host
    UEFI variables or the QEMU firmware template.
    """
    store = bytearray(blank.read_bytes())
    header, = struct.unpack_from("<H", store, 0x30)
    auth_store = UUID("aaf32c78-947b-439a-a180-2e144ec37792").bytes_le
    assert store[0x28:0x2c] == b"_FVH" and store[header:header + 16] == auth_store
    size, = struct.unpack_from("<I", store, header + 16)
    start, end = header + 28, header + size
    assert start < end <= len(store) and all(v == 0xff for v in store[start:end])

    cert = certificate.read_bytes()
    owner = UUID("6eae6341-c7c3-45cb-951a-b89f8e20c025").bytes_le
    x509 = UUID("a5c059a1-94e4-4aa7-87b5-ab155c2bf072").bytes_le
    signature = x509 + struct.pack("<III", 28 + 16 + len(cert), 0, 16 + len(cert)) + owner + cert
    global_var = UUID("8be4df61-93ca-11d2-aa0d-00e098032b8c")
    image_db = UUID("d719b2cb-3d3a-4596-a3bc-dad00e67656f")
    enable = UUID("f0a30bc7-af08-4556-99c4-001009c93a44")
    timestamp = struct.pack("<H6BIh2B", 2025, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0)

    def var(name, guid, data, attrs):
        name = (name + "\0").encode("utf-16le")
        item = (struct.pack("<HBBIQ", 0x55aa, 0x3f, 0, attrs, 0) + timestamp +
                struct.pack("<III", 0, len(name), len(data)) + guid.bytes_le + name + data)
        return item + b"\xff" * (-len(item) % 4)

    data = (var("PK", global_var, signature, 0x27) +
            var("KEK", global_var, signature, 0x27) +
            var("db", image_db, signature, 0x27) +
            var("SecureBootEnable", enable, b"\x01", 3))
    assert len(data) <= end - start
    store[start:start + len(data)] = data
    dest.write_bytes(store)


def hardware(code, vars_file):
    return ["-machine", "q35,smm=on", "-m", "256", "-global",
            "driver=cfi.pflash01,property=secure,value=on",
            "-drive", f"if=pflash,unit=0,format=raw,readonly=on,file={code}",
            "-drive", f"if=pflash,unit=1,format=raw,file={vars_file}"]


def rejected(image, options, marker, forbidden):
    qmp_port, serial_port = alloc_port(2)
    with subprocess.Popen([qemu_exe(), "-display", "none", "-nic", "none",
                           "-serial", f"tcp:127.0.0.1:{serial_port},server,wait",
                           "-qmp", f"tcp:127.0.0.1:{qmp_port},server,nowait",
                           "-drive", f"file={image},format=raw,snapshot=on", *options],
                          cwd=ROOT, stdout=subprocess.DEVNULL) as vm:
        serial = None
        try:
            serial = SerialReader(serial_port)
            qmp = Qmp(qmp_port)
            # OVMF builds report EFI_SECURITY_VIOLATION as either spelling.
            output = wait_for(lambda: serial.snapshot().replace(": Access Denied", ": Security Violation"),
                              marker)
            assert forbidden not in output, output
            qmp.quit()
            vm.wait(timeout=5)
        finally:
            if serial:
                serial.close()
            if vm.poll() is None:
                vm.kill()
                vm.wait(timeout=5)


def main():
    git = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git"
    sdk = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Windows Kits/10/bin"
    openssl = tool("EZOS_OPENSSL", [git / "usr/bin/openssl.exe", shutil.which("openssl")])
    bash = tool("EZOS_GIT_BASH", [git / "bin/bash.exe"])
    sign = tool("EZOS_SIGNTOOL", sorted(sdk.glob("*/x64/signtool.exe"), reverse=True))
    code = Path(ovmf_x64_fd()).with_name("edk2-x86_64-secure-code.fd")
    blank = code.with_name("edk2-i386-vars.fd")
    with tempfile.TemporaryDirectory(prefix="cerulia-secure-") as tmp:
        work = Path(tmp)
        key, pem, der, pfx = (work / n for n in ("test.key", "test.pem", "test.cer", "test.pfx"))
        # The password and key are for this one discarded test only.
        password = "disposable-ovmf-test"
        generated = subprocess.run(
            [openssl, "req", "-new", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-sha256", "-subj", "/CN=Cerulia Disposable OVMF Test",
             "-addext", "extendedKeyUsage=codeSigning", "-keyout", key, "-out", pem],
            capture_output=True, cwd=ROOT)
        assert generated.returncode == 0, generated.stderr.decode(errors="replace")
        run(openssl, "x509", "-in", pem, "-outform", "DER", "-out", der)
        run(openssl, "pkcs12", "-export", "-inkey", key, "-in", pem, "-out", pfx,
            "-passout", f"pass:{password}")

        embedded = work / "embedded"
        run(bash, "uefi/build.sh", "--embed-kernel", "kernel.bin", "--output-dir", embedded.as_posix())
        ia32, x64 = embedded / "BOOTIA32.EFI", embedded / "BOOTX64.EFI"
        run(sign, "sign", "/fd", "SHA256", "/f", pfx, "/p", password, ia32, x64)
        image = work / "secure.img"
        run(sys.executable, "tools/make_usb_image.py", "--require-signed", "--efi", ia32,
            "--efi-x64", x64, "-o", image)
        kernel = (ROOT / "kernel.bin").read_bytes()
        signed = x64.read_bytes()
        media = bytearray(image.read_bytes())
        esp = ESP_LBA * SECTOR
        image_off = media.find(signed, esp)
        raw_off = media.find(kernel, esp)
        assert image_off >= esp and esp <= raw_off < image_off

        # Changing the standalone payload must not change what Secure Boot runs.
        media[raw_off:raw_off + 64] = bytes(64)
        image.write_bytes(media)
        vars_file = work / "positive-vars.fd"
        provision_test_vars(blank, vars_file, der)
        boot("Secure Boot / signed embedded kernel", hardware(code, vars_file),
             ["EZEFI: firmware SecureBoot=1", "EZEFI: using PE-embedded kernel"],
             image=image, verify_user=True)

        # Changing even one authenticated byte inside the PE must be refused.
        offset = signed.find(kernel)
        assert offset >= 0
        media[image_off + offset + 32] ^= 1
        bad = work / "tampered.img"
        bad.write_bytes(media)
        vars_file = work / "negative-vars.fd"
        provision_test_vars(blank, vars_file, der)
        rejected(bad, hardware(code, vars_file), "Security Violation", "EZEFI: x64 loader")
        print("PASS Secure Boot rejects tampered embedded kernel")

        # A valid signature on the detachable-kernel loader must not be
        # enough to authenticate its separate raw file.
        detached = work / "detached-x64.efi"
        shutil.copyfile(ROOT / "uefi/esp/EFI/BOOT/BOOTX64.EFI", detached)
        run(sign, "sign", "/fd", "SHA256", "/f", pfx, "/p", password, detached)
        plain = work / "detached.img"
        run(sys.executable, "tools/make_usb_image.py", "--require-x64", "--efi-x64", detached, "-o", plain)
        vars_file = work / "detached-vars.fd"
        provision_test_vars(blank, vars_file, der)
        rejected(plain, hardware(code, vars_file), "EZEFI: Secure Boot requires an embedded kernel",
                 "EZEFI:ebs ok")
        print("PASS signed detachable-kernel loader refuses Secure Boot")


if __name__ == "__main__":
    main()
