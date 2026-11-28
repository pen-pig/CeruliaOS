"""Build the pinned, freestanding TLS archive; never download during a build."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "third_party/bearssl"
WORK = ROOT / "temp/tls-build"


def checked(path, digest):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"unexpected SHA256: {path}")
    return data


def fields(data):
    """DER TLVs from the pinned CA snapshot, never from the network peer."""
    p = 0
    while p < len(data):
        start = p
        tag, n = data[p:p+2]
        p += 2
        if n >= 128:
            size = n - 128
            assert 0 < size <= 4
            n = int.from_bytes(data[p:p+size], "big")
            p += size
        assert p + n <= len(data)
        yield tag, data[p:p+n], data[start:p+n]
        p += n


def roots(pem):
    arrays, entries = [], []

    def blob(value):
        name = f"ca_{len(arrays)}"
        arrays.append("static const unsigned char " + name + "[]={" +
                      ",".join(map(str, value)) + "};")
        return f"(unsigned char *){name},sizeof({name})"

    for der in re.findall(rb"BEGIN CERTIFICATE-----([^-]*)-----END CERTIFICATE", pem):
        cert = list(fields(next(fields(base64.b64decode(der)))[1]))
        tbs = list(fields(cert[0][1]))
        v = int(tbs[0][0] == 0xa0)
        dn = blob(tbs[v+4][2])
        alg, bits = fields(tbs[v+5][1])
        oid, *params = fields(alg[1])
        assert bits[0] == 3 and bits[1][0] == 0
        key = bits[1][1:]
        if oid[1].hex() == "2a864886f70d010101":
            n, e = fields(next(fields(key))[1])
            pk = "BR_KEYTYPE_RSA,{.rsa={" + blob(n[1].lstrip(b'\0')) + "," + blob(e[1]) + "}}"
        elif oid[1].hex() == "2a8648ce3d0201":
            curve = {"2a8648ce3d030107": 23, "2b81040022": 24, "2b81040023": 25}[params[0][1].hex()]
            pk = f"BR_KEYTYPE_EC,{{.ec={{{curve}," + blob(key) + "}}"
        else:
            raise ValueError("unsupported trust anchor key")
        entries.append("{{" + dn + "},BR_X509_TA_CA,{" + pk + "}}")
    assert entries
    return "\n".join(arrays) + "\nstatic const br_x509_trust_anchor anchors[]={\n" + ",\n".join(entries) + "};\n"


def main(cc, out):
    archive = VENDOR / "bearssl-0.6.tar.gz"
    checked(archive, "6705bba1714961b41a728dfc5debbe348d2966c117649392f8c8139efc83ff14")
    pem = checked(VENDOR / "roots.pem", "427c064216761a1e72a8b30f0d954d08cb6e4435a82091fb865afd20c8497285")
    WORK.mkdir(parents=True, exist_ok=True)
    # Only extract source and public headers; the archive hash above is pinned.
    with tarfile.open(archive) as tar:
        for member in tar:
            if member.isfile() and re.fullmatch(r"bearssl-0\.6/(inc|src)/[a-zA-Z0-9_/]+\.[ch]", member.name):
                tar.extract(member, WORK)
    src = WORK / "bearssl-0.6"
    (WORK / "roots.h").write_text(roots(pem), encoding="ascii")
    flags = ["-m32", "-march=i686", "-Os", "-ffreestanding", "-fno-pic", "-fno-pie",
             "-fno-stack-protector", "-fno-strict-aliasing", "-mno-sse", "-mno-sse2",
             "-DBR_AES_X86NI=0", "-DBR_SSE2=0", "-DBR_RDRAND=0", "-DBR_USE_URANDOM=0",
             "-DBR_USE_WIN32_RAND=0", "-DBR_USE_UNIX_TIME=0", "-DBR_USE_WIN32_TIME=0",
             "-I"+str(VENDOR / "compat"), "-I"+str(src/"src"), "-I"+str(src/"inc"), "-I"+str(WORK)]
    def compile_one(c):
        obj = WORK / (c.stem + ".o")
        subprocess.run([cc, *flags, "-c", c, "-o", obj], check=True)
        return obj.name
    with ThreadPoolExecutor(8) as pool:
        objs = list(pool.map(compile_one, [*sorted((src/"src").rglob("*.c")), VENDOR/"port.c"]))
    ar = subprocess.check_output([cc, "-print-prog-name=ar"], text=True).strip()
    subprocess.run([ar, "rcs", str(Path(out).resolve()), *objs], cwd=WORK, check=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
