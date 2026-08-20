# bioimageflow-restoration-tools

`bioimageflow-restoration-tools` provides restoration inference wrappers, simple image-processing comparisons, and image-quality metrics.
It is designed for workflows that compare noisy or blurred microscopy images with restored outputs.

Execution environments provide imageio, NumPy, SciPy, and scikit-image.
`CAREamicsPredict` runs CAREamics inference from a checkpoint.
The simple filters are documented as image-processing methods rather than learned restoration models.
SAIRPICO command-line restoration tools live in `bioimageflow-sairpico-tools`.

## Tools

- [TotalVariationDenoise](tools/total_variation_denoise.md): total-variation denoising.
- [BilateralDenoise](tools/bilateral_denoise.md): edge-preserving bilateral denoising.
- [CAREamicsPredict](tools/careamics_predict.md): CAREamics restoration prediction from a checkpoint.
- [RestorationMetrics](tools/restoration_metrics.md): compare degraded and restored images against a clean reference.
- [GaussianDenoise](tools/gaussian_denoise.md): Gaussian smoothing.
- [MedianDenoise](tools/median_denoise.md): median denoising.
- [BackgroundSubtract](tools/background_subtract.md): subtract a smoothed background estimate.
- [UnsharpMask](tools/unsharp_mask.md): sharpen by adding high-frequency residuals.
- [RichardsonLucyRestoration](tools/richardson_lucy_restoration.md): Richardson-Lucy deconvolution.

## Workflow Use

Use `CAREamicsPredict` in restoration workflows when a CAREamics checkpoint is available.
Use `RestorationMetrics` to compare noisy and restored outputs against a clean reference or curated validation crop.
