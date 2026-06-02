# RestoreImage

`RestoreImage` applies a restoration baseline to a 2D scalar image. Supported
methods are `tv_chambolle`, `bilateral`, `gaussian`, and `unsharp`; available
behavior depends on whether scikit-image is installed.

Inputs are `input_image`, `method`, and `weight`. Output is `output_image`.
With scikit-image, TV Chambolle and bilateral denoising use
`skimage.restoration`; without it, Gaussian and unsharp NumPy fallbacks keep
examples runnable.

Use it for small restoration demos and benchmark baselines. Unsupported methods
raise `ValueError`, and missing files fail through imageio.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, NumPy, and scikit-image restoration when
available. The package keeps NumPy fallbacks for deterministic tests.

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

The output image has the same shape as the input and should be less noisy for
the synthetic benchmark data.

## Failure Modes

Unsupported methods raise `ValueError`; missing inputs, unsupported formats, or
unwritable outputs fail through imageio or filesystem errors.
