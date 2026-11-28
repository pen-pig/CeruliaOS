# Cerulia native script runtime

- [QuickJS 2026-06-04](https://bellard.org/quickjs/), upstream MIT, core only (no quickjs-libc, native modules, file/shell bridge, or JIT).
- [musl 1.2.5](https://musl.libc.org/), MIT and BSD notices in COPYRIGHT.musl. Only the selected pure math implementation files are linked, not Linux libc, its syscall layer or dynamic loader.
- tools/build_webjs.py verifies the upstream hashes, stages the inputs, removes the OS-dependent atomics path, and binds QuickJS allocation-size queries to the guest allocator. The pinned archives are unchanged and no network is needed at build time.
