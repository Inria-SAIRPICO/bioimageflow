# MedianDenoise

`MedianDenoise` applies a median filter to a 2D intensity image. It uses SciPy
when available and a small NumPy fallback otherwise.

## Inputs

- `input_image`: 2D intensity image.
- `radius`: median window radius in pixels.

## Outputs

- `output_image`: median-filtered float image.

## Dependencies and Core Libraries

imageio, NumPy, SciPy median filtering when available, and a small NumPy
fallback.

## Assumptions

The image is 2D and contains impulse-like noise where local median filtering is
a reasonable baseline.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_restoration_tools import MedianDenoise

MedianDenoise().process_row(
    Arguments(input_image="noisy.tif", radius=1, output_image="median.tif")
)
```

## Expected Results

Synthetic salt-and-pepper fixtures lose isolated outliers while preserving
image shape.

## Failure Modes

Unreadable images, unsupported dimensions, invalid radius values, and write
failures raise errors.
