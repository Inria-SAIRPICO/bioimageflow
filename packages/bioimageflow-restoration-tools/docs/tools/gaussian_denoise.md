# GaussianDenoise

`GaussianDenoise` applies Gaussian smoothing to a 2D intensity image.

## Inputs

- `input_image`: 2D intensity image.
- `sigma`: Gaussian sigma in pixels.

## Outputs

- `output_image`: smoothed float image.

## Dependencies and Core Libraries

imageio, NumPy, and SciPy.

## Assumptions

The image is 2D and smoothing is intended as a baseline denoising step, not a
learned restoration model.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_restoration_tools import GaussianDenoise

GaussianDenoise().process_row(
    Arguments(input_image="noisy.tif", sigma=1.0, output_image="denoised.tif")
)
```

## Expected Results

Synthetic noisy images become smoother while preserving array shape.

## Failure Modes

Unreadable images, unsupported dimensions, invalid sigma values, and write
failures raise errors.
