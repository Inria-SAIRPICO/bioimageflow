# bioimageflow-restoration-tools

Tools for restoration inference, simple image-processing comparisons, and restoration metrics.

## Tools

- `CAREamicsPredict`: runs CAREamics restoration inference from a checkpoint.
- `RestorationMetrics`: computes MSE, PSNR, and residual-noise measurements.
- `TotalVariationDenoise`: applies Chambolle total-variation denoising.
- `BilateralDenoise`: applies edge-preserving bilateral denoising.
- `GaussianDenoise`: applies Gaussian smoothing as a simple denoising method.
- `MedianDenoise`: removes impulse noise with a SciPy median filter.
- `BackgroundSubtract`: subtracts a smoothed background estimate.
- `UnsharpMask`: sharpens images by adding high-frequency residuals.
- `RichardsonLucyRestoration`: runs Richardson-Lucy deconvolution.

Use the low-SNR restoration workflow in the main workflow catalog for a complete restoration example.

`RestoreImage` was removed in favor of the dedicated `TotalVariationDenoise`, `BilateralDenoise`, `GaussianDenoise`, and `UnsharpMask` tools.
`BackgroundSubtract.preserve_range` was renamed to `shift_to_zero` because it shifts the minimum to zero rather than preserving the original range.
