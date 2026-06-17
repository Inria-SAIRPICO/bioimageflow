# bioimageflow-restoration-tools

Tools for image restoration baselines and synthetic benchmarking.

## Tools

- `RestoreImage`: applies a scikit-image restoration baseline when scikit-image is installed. A small NumPy fallback keeps default tests and examples runnable.
- `BenchmarkRestoration`: generates a synthetic clean image, applies blur and noise, restores it, and returns PSNR/MSE metrics as dataframe columns.
- `GaussianDenoise`: applies Gaussian smoothing as a denoising baseline.
- `MedianDenoise`: removes impulse noise with a dependency-light median filter.
- `BackgroundSubtract`: subtracts a smoothed background estimate.
- `UnsharpMask`: sharpens images by adding high-frequency residuals.
- `RichardsonLucyRestoration`: runs a lightweight Richardson-Lucy deconvolution baseline.

Install scikit-image separately for evaluation runs that need the full restoration backend.

## Example

See `example-workflows/restoration_benchmark/workflow.py`.
