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
- `HotspotToSpots`: converts hotspot images to spot coordinate tables.

The SAIRPICO deconvolution CLIs expose a `-lambda` option.
Because `lambda` is a reserved Python keyword, BioImageFlow exposes this parameter as `regularization_lambda` in Python and schemas while still passing `-lambda` to the underlying CLIs.

## Environments

The package declares three `EnvironmentSpec` instances:

- `simglib`: `sylvainprigent::simglib=0.1.2`, used by PSF, deconvolution, and
  median denoising tools.
- `cimgdenoising`: `bioimageit::cimgdenoising==1.0.0`, used by
  `CImgDenoising`.
- `hotspot`: `bioimageit::hotspot==1.0.0`, used by `HotspotDetection`.

The original SAIRPICO inventory listed platform selectors for these packages:

- `simglib`: `osx-64`, `win-64`, `linux-64`.
- `cimgdenoising`: `osx-64`, `win-64`.
- `hotspot`: `osx-64`, `osx-arm64`, `win-64`, `linux-64`.

These are conda-backed command wrappers. Unit tests validate schemas, command
construction, diagnostic environment/version reports, and hotspot table
conversion without requiring the real binaries. The environment/version checks
are package diagnostics, not public BioImageFlow workflow tools. Synthetic CLI
execution is limited to subprocess monkeypatching because the SAIRPICO binaries
are not Python library calls and may not be available on every platform.
