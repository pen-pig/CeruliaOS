"""Build the native QuickJS ELF; all upstream input is pinned and vendored."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "third_party/webjs"


def release(filename,digest,where,select):
    src=VENDOR/filename
    assert hashlib.sha256(src.read_bytes()).hexdigest()==digest,filename
    with tarfile.open(src) as archive:
        for member in archive:
            base=member.name.split("/",1)
            if len(base)==2 and member.isfile() and select(base[1]):
                dst=where/base[1]
                dst.parent.mkdir(parents=True,exist_ok=True)
                dst.write_bytes(archive.extractfile(member).read())


def build(cc,ld,out):
    work=ROOT/"temp/webjs-build"
    q=work/"quickjs"
    m=work/"musl"
    release("quickjs-2026-06-04.tar.xz", "b376e839b322978313d929fd20663b11ba58b75df5a46c126dd19ea2fa70ad2a",q,
            lambda n:"/" not in n and n.endswith((".h",".c")))
    release("musl-1.2.5.tar.gz","a9a118bbe84d8764da0ea0d28b3ab3fae8477fc7e4085d90102b8596fc7c75e4",m,
            lambda n:n in ("include/math.h","include/features.h","src/internal/libm.h","arch/generic/fp_arch.h")
            or n.startswith("src/math/") and n.count("/")==2 and n.endswith((".c",".h")))
    qp=q/"quickjs.c"
    data=qp.read_text(encoding="utf-8")
    start=data.index("#if defined(__APPLE__)\n#include <malloc/malloc.h>")
    end=data.index("\n#endif",start)+len("\n#endif")
    data=data[:start]+"#ifndef CERULIA_RUNTIME\n"+data[start:end]+"\n#endif"+data[end:]
    old="#if !defined(__EMSCRIPTEN__)\n#define CONFIG_ATOMICS"
    assert data.count(old)==1
    data=data.replace(old,"#if !defined(__EMSCRIPTEN__) && !defined(CERULIA_RUNTIME)\n#define CONFIG_ATOMICS")
    old="static size_t js_def_malloc_usable_size(const void *ptr)\n{"
    assert data.count(old)==1
    start=data.index(old)+len(old)
    stop=data.index("\n}",start)
    data=data[:start]+"\n    return web_usable(ptr);"+data[stop:]
    data=data.replace("#define malloc(s) malloc_is_forbidden(s)",
                      "#undef malloc\n#undef free\n#define malloc(s) malloc_is_forbidden(s)")
    qp.write_text(data,encoding="utf-8")
    mh=m/"include/math.h"
    data=mh.read_text(encoding="utf-8")
    mh.write_text(data.replace("#include <features.h>", "#include <features.h>\n#include <float.h>\n#undef NAN\n#undef INFINITY"),encoding="utf-8")
    compat=ROOT/"user/webcompat"
    alloc=work/"alloc.h"
    alloc.write_text("#include <stdlib.h>\n"
                     "void *web_alloc(size_t); void web_free(void *); size_t web_usable(const void *);\n"
                     "#define malloc web_alloc\n#define free web_free\n",encoding="ascii")
    compiler_headers=subprocess.check_output([cc,"-m32","-print-file-name=include"],text=True).strip()
    flags=["-m32","-march=i686","-std=gnu11","-Os","-ffreestanding","-fno-pic","-fno-pie",
           "-fno-stack-protector","-fno-strict-aliasing","-ffunction-sections","-fdata-sections",
           "-mno-sse","-mno-sse2","-mno-mmx","-mfpmath=387","-fexcess-precision=standard",
           "-nostdinc","-isystem",compiler_headers,
           "-I"+str(compat),"-I"+str(m/"include"),"-I"+str(m/"arch/generic"),
           "-I"+str(m/"src/internal"),"-I"+str(q)]
    units=[(q/n,[ "-D_GNU_SOURCE","-DCERULIA_RUNTIME",
                 '-DCONFIG_VERSION="2026-06-04"',"-include",str(alloc)]) for n in
           ("quickjs.c","libregexp.c","libunicode.c","cutils.c","dtoa.c")]
    math="""acos acosh asin asinh atan atan2 atanh cbrt ceil cos cosh exp expm1 fabs floor fmax fmin
    fmod hypot log log10 log1p log2 pow round sin sinh sqrt tan tanh trunc scalbn scalbnl copysign
    __math_divzero __math_invalid __math_oflow __math_uflow __math_xflow __math_check_oflow __math_check_uflow
    __rem_pio2 __rem_pio2_large __cos __sin __tan sqrt_data exp_data log_data log2_data pow_data
    lrint rint sqrtl __expo2 __math_invalidl""".split()
    units += [(m/"src/math"/(n+".c"),["-Dhidden=__attribute__((visibility(\"hidden\")))"])
              for n in math if (m/"src/math"/(n+".c")).exists()]
    units += [(ROOT/"user"/n,flag) for n,flag in
              (("webcompat.c",[]),("webjs.c",[]),("libc.c",["-DHEAP_SIZE=2621440"]))]
    def compile_unit(item):
        src,extra=item
        dst=work/(src.stem+".o")
        subprocess.run([cc,*flags,*extra,"-c",src,"-o",dst],check=True)
        return dst
    with ThreadPoolExecutor(8) as pool:
        objs=list(pool.map(compile_unit,units))
    support=subprocess.check_output([cc,"-m32","-print-libgcc-file-name"],text=True).strip()
    subprocess.run([ld,"-m","elf_i386","--gc-sections","-s","-T",ROOT/"user/user.ld",
                    "-o",out,ROOT/"user/crt0.o",*objs,support],check=True)


if __name__=="__main__":
    build(*sys.argv[1:])
