# RestoreImage

`RestoreImage` applies a simple image-processing restoration method to a 2D scalar image. Supported
methods are `tv_chambolle`, `bilateral`, `gaussian`, and `unsharp`; available
behavior depends on whether scikit-image is installed.

Inputs are `input_image`, `method`, and `weight`. Output is `output_image`.
With scikit-image, TV Chambolle and bilateral denoising use
`skimage.restoration`; Gaussian and unsharp are implemented with NumPy.

Use it for simple image-processing comparisons. Unsupported methods raise
`ValueError`, and missing files fail through imageio.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, NumPy, and scikit-image restoration when
available.

## Assumptions

The input is a 2D scalar image normalized to a range where the selected method
and `weight` are meaningful.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_restoration_tools import RestoreImage

RestoreImage().process_row(
    Arguments(input_image="noisy.tif", method="tv_chambolle", weight=0.12)
)
```

## Expected Results

The output image has the same shape as the input. Compare it with
`RestorationMetrics` when a clean or high-SNR reference is available.

## Failure Modes

Unsupported methods raise `ValueError`; missing inputs, unsupported formats, or
unwritable outputs fail through imageio or filesystem errors.
