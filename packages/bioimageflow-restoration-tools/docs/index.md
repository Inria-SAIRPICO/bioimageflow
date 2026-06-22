# bioimageflow-restoration-tools

`bioimageflow-restoration-tools` provides restoration inference wrappers, simple image-processing baselines, and image-quality metrics.
It is designed for workflows that compare noisy or blurred microscopy images with restored outputs.

Install-time libraries are imageio and NumPy.
`CAREamicsPredict` runs CAREamics inference from a checkpoint.
The simple filters are documented as image-processing baselines rather than learned restoration models.
SAIRPICO command-line restoration tools live in `bioimageflow-sairpico-tools`.

## Tools

- [RestoreImage](tools/restore_image.md): apply a TV, bilateral, Gaussian, or
  unsharp-style restoration baseline.
- [CAREamicsPredict](tools/careamics_predict.md): CAREamics restoration prediction from a checkpoint.
- [RestorationMetrics](tools/restoration_metrics.md): compare degraded and restored images against a clean reference.
- [GaussianDenoise](tools/gaussian_denoise.md): Gaussian smoothing baseline.
- [MedianDenoise](tools/median_denoise.md): median denoising baseline.
- [BackgroundSubtract](tools/background_subtract.md): subtract a smoothed background
  estimate.
- [UnsharpMask](tools/unsharp_mask.md): sharpen by adding high-frequency residuals.
- [RichardsonLucyRestoration](tools/richardson_lucy_restoration.md): lightweight
  Richardson-Lucy deconvolution baseline.

## Workflow Use

Use `CAREamicsPredict` in restoration workflows when a CAREamics checkpoint is available.
Use `RestorationMetrics` to compare noisy and restored outputs against a clean reference or curated validation crop.
