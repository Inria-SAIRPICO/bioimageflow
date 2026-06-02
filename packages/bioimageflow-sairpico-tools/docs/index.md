# BioImageFlow SAIRPICO Tools

`bioimageflow-sairpico-tools` owns BioImageFlow wrappers for the legacy
SAIRPICO command-line programs. The package exposes point-spread-function
generation, deconvolution, denoising, and hotspot detection tools while keeping
the original binaries in conda-backed `EnvironmentSpec` definitions.

The public tools are:

- [GaussianPSF](#gaussianpsf): generate a 3D Gaussian PSF with
  `simggaussian3dpsf`.
- [GibsonLanniPSF](#gibsonlannipsf): generate a 3D Gibson-Lanni PSF
  with `simggibsonlannipsf`.
- [RichardsonLucyDeconvolution](#richardsonlucydeconvolution): run
  2D, 2D-slice, or 3D Richardson-Lucy deconvolution.
- [WienerDeconvolution](#wienerdeconvolution): run 2D, 2D-slice, or
  3D Wiener deconvolution.
- [SpitfireDeconvolution](#spitfiredeconvolution): run 2D, 2D-slice,
  or 3D SPITFIR(e) deconvolution.
- [MedianDenoising](#mediandenoising): run 2D, 3D, or 4D median
  filtering.
- [CImgDenoising](#cimgdenoising): run the CImg `denoise` command.
- [HotspotDetection](#hotspotdetection): run `hotSpotDetection` for
  sparse hotspot detection.
- [HotspotToSpots](#hotspottospots): convert hotspot images into spot tables.

The package declares three environments:

- `simglib`: `sylvainprigent::simglib=0.1.2` from `conda-forge` and
  `sylvainprigent`; used by PSF, deconvolution, and median-denoising tools.
- `cimgdenoising`: `bioimageit::cimgdenoising==1.0.0` from `conda-forge` and
  `bioimageit`; used by `CImgDenoising`.
- `hotspot`: `bioimageit::hotspot==1.0.0` from `conda-forge` and `bioimageit`;
  used by `HotspotDetection`.

Inputs are legacy image files, with public image wrappers declaring `png`,
`tif`, and `tiff` formats. The underlying SAIRPICO binaries are external
processes, so failures are usually surfaced as missing binaries, missing PSF
files for 3D deconvolution, unsupported file layouts, or non-zero subprocess
exit codes.

Environment availability and version reporting are covered by package
diagnostics and tests instead of being exposed as public workflow tools. They do
not produce image-analysis outputs and should not appear in analyst-facing
workflow builders.

Demo workflow:

- [SAIRPICO restoration smoke](#sairpico-restoration-smoke-workflow)
