# RichardsonLucyRestoration

`RichardsonLucyRestoration` applies a lightweight Richardson-Lucy
deconvolution baseline to a 2D intensity image.

## Inputs

- `input_image`: 2D intensity image.
- `psf_image`: optional 2D point-spread-function image.
- `iterations`: number of Richardson-Lucy updates.
- `clip`: optionally clip output to `[0, 1]`.

## Outputs

- `output_image`: restored float image.

## Dependencies and Core Libraries

imageio, NumPy, and the package-local Richardson-Lucy baseline.

## Assumptions

This is a lightweight public-library baseline. It is useful for demonstrative
deblurring tests but should not replace validated microscope-specific
deconvolution workflows.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_restoration_tools import RichardsonLucyRestoration

RichardsonLucyRestoration().process_row(
    Arguments(input_image="blurred.tif", iterations=10, output_image="restored.tif")
)
```

## Expected Results

Synthetic blurred fixtures recover sharper peaks than the input under the same
array shape.

## Failure Modes

Unreadable images, PSF shape mismatches, invalid iteration counts, and write
failures raise errors.
