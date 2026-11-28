# TJpgDec

ChaN's [TJpgDec R0.03](https://elm-chan.org/fsw/tjpgd/00index.html), downloaded from the upstream release archive (`arc/tjpgd3.zip`). The embedded copyright and redistribution terms are in `tjpgd.c` and `tjpgd.h`. CeruliaOS selects RGB565 in `tjpgdcnf.h` and normalizes line endings; the decoder is unchanged. It is used for reduced-scale baseline JPEG decoding when a full-resolution scratch image will not fit. PNG and small/progressive JPEG retain the existing Wuffs path.

Upstream archive SHA-256: `052fe3efbc9a8be29f31597ad009c5b51a4f6905878eb28569e0ab3d46d0c013`.
