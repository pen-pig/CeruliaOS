"""Boot and run a shell command on several minimal QEMU hardware profiles."""
import argparse
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ezos_env import ovmf_fd, ovmf_x64_fd, qemu_exe  # noqa: E402
from ezos_qemu import alloc_port, image_path  # noqa: E402
from test_nvme import Qmp, SerialReader  # noqa: E402

VER = "Cerulia Kernel version "


def wait_for(read_output, marker, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        output = read_output()
        found = marker(output) if callable(marker) else marker in output
        if found:
            return output
        if "System halted" in output:
            break
        time.sleep(0.1)
    raise AssertionError(f"missing {marker!r}:\n{read_output()[-3500:]}")


def type_on_usb(qmp, read_output, line, prefix, completed):
    # Keep each key pressed until the guest polls and acknowledges it. HMP
    # sendkey uses a timed release that can expire between USB polls under load.
    keys = [*line, "ret"]
    for i, key in enumerate(keys, 1):
        def event(down):
            reply = qmp.cmd("input-send-event", events=[{"type": "key", "data": {
                "down": down, "key": {"type": "qcode", "data": key},
            }}])
            assert "return" in reply, reply

        event(True)
        try:
            if key == "ret":
                wait_for(read_output, completed)
            else:
                wait_for(read_output, lambda t: t.endswith(prefix + line[:i]))
        finally:
            event(False)
        # The HID driver must see a key-up report before a repeated key.
        time.sleep(0.1)


def boot(label, hardware, expected=(), rescue=False, uart=True, image=None, warm=False,
         usb_keyboard=False, usb_boot=False, verify_user=False, after=None):
    qmp_port, com = alloc_port(2)
    with tempfile.TemporaryDirectory(prefix="cerulia-boot-") as scratch:
        vram = Path(scratch) / "vram.bin"
        com_arg = f"tcp:127.0.0.1:{com},server,wait" if uart else "none"
        disk = f"file={image or image_path()},format=raw,snapshot=on"
        if usb_boot:
            storage = ["-device", "qemu-xhci,id=usb", "-drive", disk + ",if=none,id=esp",
                       "-device", "usb-storage,bus=usb.0,drive=esp,bootindex=1"]
        else:
            storage = ["-drive", disk]
        with subprocess.Popen([
            qemu_exe(), "-display", "none", "-nic", "none", "-serial", com_arg,
            "-qmp", f"tcp:127.0.0.1:{qmp_port},server,nowait",
            *storage,
            *hardware,
        ], cwd=ROOT, stdout=subprocess.DEVNULL) as vm:
            serial = None
            try:
                if uart:
                    serial = SerialReader(com)
                qmp = Qmp(qmp_port)

                def read_vga():
                    reply = qmp.hmc(f'pmemsave 0xb8000 4000 "{vram.as_posix()}"')
                    assert not reply.get("return") and vram.is_file(), reply
                    return vram.read_bytes()[::2].decode("ascii", "replace")

                read = serial.snapshot if serial else read_vga
                output = wait_for(read, "Login: ")
                for marker in expected:
                    assert marker in output, f"{label}: {marker!r} missing:\n{output[-3500:]}"
                if rescue:
                    serial.sock.sendall(b"root\r\n")
                elif usb_keyboard:
                    type_on_usb(qmp, read, "root", "", "[root:")
                else:
                    qmp.type_line("root")
                wait_for(read, "[root:")
                if rescue:
                    serial.sock.sendall(b"vey\x7fr\r\n")  # backspace and CRLF must work
                elif usb_keyboard:
                    type_on_usb(qmp, read, "ver", "] > ", VER)
                else:
                    qmp.type_line("ver")
                wait_for(read, VER)
                if verify_user:
                    start = serial.size()
                    qmp.type_line("exec /bin/hello.elf")
                    wait_for(lambda: serial.tail_from(start), "exited with code 42")
                if warm:
                    start = serial.size()
                    qmp.type_line("reboot")
                    restarted = lambda: serial.tail_from(start)
                    wait_for(restarted, "Login: ")
                    qmp.type_line("root")
                    wait_for(restarted, "[root:")
                    qmp.type_line("ver")
                    wait_for(restarted, VER)
                if after:
                    after(qmp, serial, Path(scratch))
                qmp.quit()
                vm.wait(timeout=5)
            finally:
                if serial:
                    serial.close()
                if vm.poll() is None:
                    vm.kill()
                    vm.wait(timeout=5)
    print(f"PASS {label}: reached shell and ran ver", flush=True)


def verify_gop_and_mouse(qmp, serial, scratch):
    shot = scratch / "gop.ppm"
    reply = qmp.cmd("screendump", filename=str(shot))
    assert "return" in reply, reply
    magic, dimensions, depth, pixels = shot.read_bytes().split(b"\n", 3)
    width, height = map(int, dimensions.split())
    assert magic == b"P6" and depth == b"255" and (width, height) == (800, 600)
    assert len(pixels) == 3 * width * height
    font = bytes(int(n, 16) for n in re.findall(r"0x([0-9a-fA-F]{2})\b",
                                                (ROOT / "kernel/vga_font.h").read_text()))
    assert len(font) == 4096

    def matches(row, col, text):
        for i, c in enumerate(text):
            for y in range(16):
                bits = font[ord(c) * 16 + y]
                at = 3 * ((row * 16 + y) * width + (col + i) * 8)
                for x in range(8):
                    lit = max(pixels[at + 3*x:at + 3*x + 3]) > 16
                    if lit != bool(bits & (0x80 >> x)):
                        return False
        return True

    # This asserts the actual framebuffer glyphs, not just a serial message.
    assert any(matches(row, 0, "Cerulia Kernel version") for row in range(25)), (
        "ver output was not drawn on the GOP framebuffer")
    start = serial.size()
    result = qmp.cmd("input-send-event", events=[
        {"type": "rel", "data": {"axis": "x", "value": 12}},
        {"type": "rel", "data": {"axis": "y", "value": -7}},
    ])
    assert "return" in result, result
    wait_for(lambda: serial.tail_from(start), "USB-MOU: move dx=12 dy=-7")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--usb-image", type=Path, help="also test the hybrid image in BIOS and both UEFI architectures")
    options = parser.parse_args()
    boot("BIOS / IDE / PS/2 (cold and warm)", ["-machine", "pc", "-m", "128"],
         ["Keyboard: PS/2 keyboard initialized"], warm=True)
    boot("BIOS / no PS/2 or VGA", ["-machine", "pc,i8042=off", "-m", "64", "-vga", "none"],
         ["PS/2 controller absent or unresponsive", "COM1 rescue input active"], rescue=True)
    boot("BIOS / USB keyboard only", ["-machine", "pc,i8042=off", "-m", "128",
                                     "-device", "piix3-usb-uhci", "-device", "usb-kbd"],
         ["PS/2 controller absent or unresponsive", "Keyboard: using USB HID"], usb_keyboard=True)
    boot("BIOS / Q35 SATA / no APIC or MSR", ["-machine", "q35", "-m", "64",
                                            "-cpu", "qemu32,-apic,-msr"],
         ["AHCI: controller 8086:2922", "AHCI: port 0 online", "APIC/MSR: not available"])
    boot("BIOS / no UART", ["-machine", "pc", "-m", "64"], uart=False)
    if options.usb_image:
        drive = options.usb_image.resolve()
        firmware = ["-drive", f"if=pflash,format=raw,readonly=on,file={ovmf_fd()}"]
        boot("BIOS / hybrid image", ["-machine", "pc", "-m", "128"],
             ["stage2: kernel image read by BIOS"], image=drive)
        boot("IA32 UEFI / no PS/2", ["-machine", "pc,i8042=off", "-m", "256", *firmware],
             ["EZEFI:ebs ok", "PS/2 controller absent or unresponsive",
              "COM1 rescue input active"], rescue=True, image=drive)
        boot("IA32 UEFI / no UART", ["-machine", "pc", "-m", "256", *firmware],
             uart=False, image=drive)
        firmware_x64 = ["-drive", f"if=pflash,format=raw,readonly=on,file={ovmf_x64_fd()}"]
        markers = ["EZEFI: x64 loader", "EZEFI:ebs ok", "EZEFI:x64 entering i686",
                   "Memory: UEFI GetMemoryMap handoff applied"]
        boot("x64 UEFI / Q35 SATA", ["-machine", "q35", "-m", "256", *firmware_x64],
             markers, image=drive, verify_user=True)
        boot("x64 UEFI / removable USB / serial rescue",
             ["-machine", "q35,i8042=off", "-m", "256", *firmware_x64],
             markers + ["COM1 rescue input active"], rescue=True, image=drive, usb_boot=True)
        boot("x64 UEFI / pure GOP / xHCI keyboard and mouse",
             ["-machine", "q35,i8042=off", "-m", "256", "-vga", "none", "-device", "ramfb",
              *firmware_x64, "-device", "qemu-xhci,id=xhci", "-device", "usb-kbd,bus=xhci.0",
              "-device", "usb-mouse,bus=xhci.0"],
             markers + ["Console: GOP framebuffer text console active", "Keyboard: using USB HID",
                        "USB HID boot mouse active", "xHCI: using PAE window for BAR above 4 GiB"],
             image=drive, usb_keyboard=True, after=verify_gop_and_mouse)


if __name__ == "__main__":
    main()
