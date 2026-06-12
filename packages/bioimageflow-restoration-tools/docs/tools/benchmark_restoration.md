# BenchmarkRestoration

`BenchmarkRestoration` creates a synthetic clean image, applies blur and noise, restores the degraded image, and returns PSNR/MSE metrics.

Inputs are `image_size`, `noise_sigma`, `blur_sigma`, and `seed`.
Outputs are `clean_image`, `degraded_image`, `restored_image`, `mse_degraded`, `mse_restored`, `degraded_psnr`, and `restored_psnr`.
No metrics CSV artifact is written because BioImageFlow records these metrics in the output dataframe.

Use it as a deterministic demo and regression test for restoration workflows.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, NumPy, and scikit-image restoration when available.

## Assumptions

The generated image is a synthetic proxy for restoration behavior and should not be interpreted as a biological validation dataset.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_restoration_tools import BenchmarkRestoration

BenchmarkRestoration().process_row(Arguments(image_size=48, seed=7))
```

## Expected Results

All three output image files exist, and `restored_psnr` is expected to be greater than `degraded_psnr` for the default generated fixture.

## Failure Modes

Invalid output paths, unwritable directories, or unavailable optional restoration dependencies can fail execution.
The NumPy fallback covers the fast test path when scikit-image restoration is not installed.
