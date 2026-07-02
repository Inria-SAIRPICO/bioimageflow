# MedianDenoising

## Purpose

`MedianDenoising` applies SAIRPICO median filtering through `simgmedian2d`,
`simgmedian3d`, or `simgmedian4d`. It is a simple restoration and preprocessing
tool for removing sparse impulse-like noise before segmentation, spot
detection, or deconvolution.

## Inputs

- `input_image`: intensity image in `png`, `tif`, or `tiff` format.
- `denoising_type`: `2D`, `3D`, or `4D`. Default `2D`.
- `radius_x`: median-filter radius in X. Default `2`.
- `radius_y`: median-filter radius in Y. Default `2`.
- `radius_z`: median-filter radius in Z for `3D` and `4D` modes. Default `1`.
- `radius_t`: median-filter radius in time for `4D` mode. Default `1`.
- `padding`: whether to process border pixels using padding. Default `False`.

## Outputs

- `output_image`: median-filtered intensity image. The default template is
  `{input_image.stem}_filtered{ext}`.

## Assumptions

- The selected dimensional mode matches the input image layout.
- Radius values are tuned to preserve objects of interest.
- `radius_z` is ignored in `2D` mode and `radius_t` is used only in `4D` mode.

## Dependencies and Core Libraries

- BioImageFlow core tool abstractions.
- SAIRPICO `simglib` environment with `bioimageit::simglib==0.1.2`.
- External commands: `simgmedian2d`, `simgmedian3d`, and `simgmedian4d`.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_sairpico_tools import MedianDenoising

result = MedianDenoising().process_row(
    Arguments(
        input_image="input.tif",
        denoising_type="2D",
        radius_x=1,
        radius_y=1,
        radius_z=1,
        radius_t=1,
        padding=True,
        output_image="median.tif",
    )
)
```

## Expected Results

The wrapper writes `median.tif` and returns that path. The example invokes
`simgmedian2d` with `-rx 1`, `-ry 1`, and `-padding true`.

## Failure Modes

- The selected `simgmedian*` command is missing.
- Image dimensionality does not match the selected mode.
- Radius values remove desired structures or create excessive smoothing.
- The subprocess exits non-zero or cannot write the output.
