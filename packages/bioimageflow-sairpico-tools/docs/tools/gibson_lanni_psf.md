# GibsonLanniPSF

## Purpose

`GibsonLanniPSF` generates a 3D Gibson-Lanni point-spread-function image with
SAIRPICO `simggibsonlannipsf`. Use it when deconvolution needs a synthetic PSF
parameterized by wavelength, pixel size, numerical aperture, refractive indices,
and working distance.

## Inputs

- `width`: output image width in pixels. Default `256`.
- `height`: output image height in pixels. Default `256`.
- `depth`: number of Z planes. Default `20`.
- `wavelength`: excitation wavelength in nanometers. Default `610.0`.
- `psxy`: XY pixel size in nanometers. Default `100.0`.
- `psz`: Z pixel size in nanometers. Default `250.0`.
- `na`: numerical aperture. Default `1.4`.
- `ni`: immersion refractive index. Default `1.5`.
- `ns`: sample refractive index. Default `1.3`.
- `ti`: working distance in micrometers. Default `150.0`.

## Outputs

- `output_image`: generated 3D intensity TIFF. The default template is
  `gibson_lanni_psf.tif`.

## Assumptions

- Input optical parameters are already in the units expected by SAIRPICO.
- The output is intended for 3D deconvolution tools that accept volumetric PSFs.
- The wrapper does not estimate parameters from acquisition metadata.

## Dependencies and Core Libraries

- BioImageFlow core tool and image schema classes.
- SAIRPICO `simglib` environment with `bioimageit::simglib==0.1.2`.
- External command: `simggibsonlannipsf`.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_sairpico_tools import GibsonLanniPSF

result = GibsonLanniPSF().process_row(
    Arguments(
        width=64,
        height=64,
        depth=16,
        wavelength=610.0,
        psxy=100.0,
        psz=250.0,
        na=1.4,
        ni=1.5,
        ns=1.3,
        ti=150.0,
        output_image="gibson_lanni_psf.tif",
    )
)
```

## Expected Results

The output path is created and returned as `output_image`. The generated file is
a volumetric intensity TIFF suitable for tools such as 3D Richardson-Lucy,
Wiener, or SPITFIR(e) deconvolution.

## Failure Modes

- `simggibsonlannipsf` is unavailable in the active environment.
- Invalid physical parameters cause the external binary to fail.
- Output path creation or writing fails.
- Requested image size is too large for available resources.
