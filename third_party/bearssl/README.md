# Cerulia guest TLS

The guest links the upstream [BearSSL 0.6 source release](https://www.bearssl.org/bearssl-0.6.tar.gz) (MIT; full license in the archive). The CA input is the [2026-11-05 Mozilla bundle distributed by curl](https://curl.se/ca/cacert-2026-11-05.pem) (MPL-2.0). Both SHA-256 values are pinned in `tools/build_tls.py`; builds do not need the network, host certificate store or an HTTPS proxy. When updating the bundle, verify its published digest, review trust changes, update the pin and repeat the acceptance tests. Do not add a test CA to the production bundle.

`port.c` uses the kernel TCP socket, a valid guest RTC (configure it as UTC), fresh CPU RDRAND entropy, TLS 1.2 ECDHE with AES-GCM or ChaCha20-Poly1305, and BearSSL's X.509 validator with SNI and the requested DNS name. No plaintext fallback. No RDRAND or invalid clock means failure, not a seeded timestamp or skipped validation. Numeric HTTPS authorities, TLS 1.3-only sites, certificate revocation checking and JavaScript applications are not supported.

On VMware, use a connected **E1000 / NAT** adapter. On QEMU, `-cpu max` exposes the random source. From an administrator session run `desktop`, then **B**, **F1**, enter `bing.com`. Use an explicit `http://` for plaintext. The shell's `fetch https://bing.com/ bing.htm` exercises the same transport without the renderer.

After building, run `python tests/test_browser_network.py` for the local slow-server/UI test. Install Python `cryptography` to include the untrusted-certificate/no-GET test. Use `--live` to additionally check the public Bing endpoint in both the shell and desktop.
