# bioimageflow-restoration-tools

Tools for restoration inference, simple image-processing comparisons, and restoration metrics.

## Tools

- `CAREamicsPredict`: runs CAREamics restoration inference from a checkpoint.
- `RestoreImage`: applies a simple image-processing restoration method.
- `RestorationMetrics`: computes MSE, PSNR, and residual-noise measurements.
- `GaussianDenoise`: applies Gaussian smoothing as a simple denoising method.
- `MedianDenoise`: removes impulse noise with a dependency-light median filter.
- `BackgroundSubtract`: subtracts a smoothed background estimate.
- `UnsharpMask`: sharpens images by adding high-frequency residuals.
- `RichardsonLucyRestoration`: runs Richardson-Lucy deconvolution.

Use the low-SNR restoration workflow in the main workflow catalog for a complete restoration example.
