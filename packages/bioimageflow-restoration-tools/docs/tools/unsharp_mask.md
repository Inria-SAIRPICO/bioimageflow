# UnsharpMask

`UnsharpMask` sharpens a 2D intensity image by adding a high-frequency residual
back to the original image.

## Inputs

- `input_image`: 2D intensity image.
- `sigma`: Gaussian sigma for the low-pass image.
- `amount`: residual scaling factor.

## Outputs

- `output_image`: sharpened float image.

## Dependencies and Core Libraries

imageio, NumPy, and scikit-image.

## Assumptions

Moderate sharpening improves inspection or downstream spot detection; excessive
amount values can amplify noise.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_restoration_tools import UnsharpMask

UnsharpMask().process_row(
    Arguments(input_image="soft.tif", sigma=1.0, amount=0.8, output_image="sharp.tif")
)
```

## Expected Results

Synthetic blurred-edge fixtures show stronger local contrast while preserving
image shape.

## Failure Modes

Unreadable images, unsupported dimensions, invalid parameters, and write
failures raise errors.
