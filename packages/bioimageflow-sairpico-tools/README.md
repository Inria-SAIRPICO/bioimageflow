# bioimageflow-sairpico-tools

BioImageFlow wrappers for the SAIRPICO command-line tools from
`bioimageit/PyFlow/Tools/Sairpico`.

## Tools

- `GaussianPSF`: wraps `simggaussian3dpsf`.
- `GibsonLanniPSF`: wraps `simggibsonlannipsf`.
- `RichardsonLucyDeconvolution`: wraps `simgrichardsonlucy2d`,
  `simgrichardsonlucy2dslice`, and `simgrichardsonlucy3d`.
- `WienerDeconvolution`: wraps `simgwiener2d`, `simgwiener2dslice`, and
  `simgwiener3d`.
- `SpitfireDeconvolution`: wraps `simgspitfiredeconv2d`,
  `simgspitfiredeconv2dslice`, and `simgspitfiredeconv3d`.
- `MedianDenoising`: wraps `simgmedian2d`, `simgmedian3d`, and `simgmedian4d`.
- `CImgDenoising`: wraps `denoise`.
- `HotspotDetection`: wraps `hotSpotDetection`.

The legacy `lambda` deconvolution parameter is exposed as
`regularization_lambda` in Python and BioImageFlow schemas, while the wrappers
still pass `-lambda` to the underlying CLIs.

## Environments

The package declares three `EnvironmentSpec` instances:

- `simglib`: `sylvainprigent::simglib=0.1.2`, used by PSF, deconvolution, and
  median denoising tools.
- `cimgdenoising`: `bioimageit::cimgdenoising==1.0.0`, used by
  `CImgDenoising`.
- `hotspot`: `bioimageit::hotspot==1.0.0`, used by `HotspotDetection`.

The legacy SAIRPICO inventory listed platform selectors for these packages:

- `simglib`: `osx-64`, `win-64`, `linux-64`.
- `cimgdenoising`: `osx-64`, `win-64`.
- `hotspot`: `osx-64`, `osx-arm64`, `win-64`, `linux-64`.

These are conda-backed command wrappers. Unit tests validate schemas and command
construction without requiring the real binaries. Synthetic execution is limited
to subprocess monkeypatching because the SAIRPICO binaries are not Python
library calls and may not be available on every platform.
