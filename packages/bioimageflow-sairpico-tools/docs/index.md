# BioImageFlow SAIRPICO Tools

`bioimageflow-sairpico-tools` owns BioImageFlow wrappers for the SAIRPICO
command-line programs. The package exposes point-spread-function
generation, deconvolution, denoising, and hotspot detection tools while keeping
the original binaries in conda-backed `EnvironmentSpec` definitions.

The public tools are:

- [GaussianPSF](tools/gaussian_psf.md): generate a 3D Gaussian PSF with
  `simggaussian3dpsf`.
- [GibsonLanniPSF](tools/gibson_lanni_psf.md): generate a 3D Gibson-Lanni PSF
  with `simggibsonlannipsf`.
- [RichardsonLucyDeconvolution](tools/richardson_lucy_deconvolution.md): run
  2D, 2D-slice, or 3D Richardson-Lucy deconvolution.
- [WienerDeconvolution](tools/wiener_deconvolution.md): run 2D, 2D-slice, or
  3D Wiener deconvolution.
- [SpitfireDeconvolution](tools/spitfire_deconvolution.md): run 2D, 2D-slice,
  or 3D SPITFIR(e) deconvolution.
- [MedianDenoising](tools/median_denoising.md): run 2D, 3D, or 4D median
  filtering.
- [CImgDenoising](tools/cimg_denoising.md): run the CImg `denoise` command.
- [HotspotDetection](tools/hotspot_detection.md): run `hotSpotDetection` for
  sparse hotspot detection.
- [HotspotToSpots](tools/hotspot_to_spots.md): convert hotspot images into spot tables.

The package declares three environments:

- `simglib`: `bioimageit::simglib==0.1.2` from `conda-forge` and
  `bioimageit`; used by PSF, deconvolution, and median-denoising tools.
- `cimgdenoising`: `bioimageit::cimgdenoising==1.0.0` from `conda-forge` and
  `bioimageit`; used by `CImgDenoising`.
- `hotspot`: `bioimageit::hotspot==1.0.0` plus pinned PyPI NumPy, SciPy, imageio, and tifffile dependencies; used by `HotspotDetection` and `HotspotToSpots`; Linux additionally pins the libtiff 4.4 ABI required by the published Linux executable.

Inputs are image files, with public image wrappers declaring `png`, `tif`, and `tiff` formats.
Image outputs default to fixed `.tif` paths, and mode values map to an explicit executable allowlist.
The wrappers validate direct-call parameters before starting external processes, so failures are surfaced as clear validation errors, missing binaries, missing PSF files for 3D deconvolution, unsupported file layouts, or non-zero subprocess exit codes.

Environment availability and version reporting are covered by package
diagnostics and tests instead of being exposed as public workflow tools. They do
not produce image-analysis outputs and should not appear in analyst-facing
workflow builders.

Workflow use:

Use the SAIRPICO deconvolution workflow in the main workflow catalog for PSF generation, denoising, deconvolution, and metrics.
