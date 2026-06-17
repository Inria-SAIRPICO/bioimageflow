# BackgroundSubtract

`BackgroundSubtract` subtracts a Gaussian-smoothed background estimate from a
2D intensity image.

## Inputs

- `input_image`: 2D intensity image.
- `sigma`: Gaussian sigma used for background estimation.
- `preserve_range`: shift output so the minimum is zero.

## Outputs

- `output_image`: background-subtracted float image.

## Dependencies and Core Libraries

imageio, NumPy, and the package-local Gaussian smoothing helper.

## Assumptions

The background varies more slowly than foreground objects at the configured
sigma.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_restoration_tools import BackgroundSubtract

BackgroundSubtract().process_row(
    Arguments(input_image="image.tif", sigma=8.0, output_image="corrected.tif")
)
```

## Expected Results

Synthetic gradient-background fixtures have reduced low-frequency background
and preserve bright foreground structures.

## Failure Modes

Unreadable images, unsupported dimensions, invalid sigma values, and write
failures raise errors.
