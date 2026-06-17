# bioimageflow-restoration-tools

`bioimageflow-restoration-tools` provides small restoration baselines and a
synthetic benchmark. It is designed for demonstrative workflows that show how
image quality changes can be measured, not as a full production restoration
suite.

Install-time libraries are imageio and NumPy.
`RestoreImage` and `BenchmarkRestoration` use optional scikit-image restoration helpers when available and fall back to package-local NumPy baselines for deterministic tests.
SAIRPICO command-line restoration tools live in `bioimageflow-sairpico-tools`.

## Tools

- <a href="tools/restore_image.md">RestoreImage</a>: apply a TV, bilateral, Gaussian, or
  unsharp-style restoration baseline.
- <a href="tools/benchmark_restoration.md">BenchmarkRestoration</a>: generate a synthetic clean/degraded/restored benchmark and dataframe metrics.
- <a href="tools/gaussian_denoise.md">GaussianDenoise</a>: Gaussian smoothing baseline.
- <a href="tools/median_denoise.md">MedianDenoise</a>: median denoising baseline.
- <a href="tools/background_subtract.md">BackgroundSubtract</a>: subtract a smoothed background
  estimate.
- <a href="tools/unsharp_mask.md">UnsharpMask</a>: sharpen by adding high-frequency residuals.
- <a href="tools/richardson_lucy_restoration.md">RichardsonLucyRestoration</a>: lightweight
  Richardson-Lucy deconvolution baseline.

## Demo Workflow

- <a href="workflows/restoration_benchmark.md">Restoration benchmark workflow</a>:
  generate clean/degraded/restored images and verify metric improvement.

## Tests and Demo Data

Run package tests with:

```bash
uv run pytest packages/bioimageflow-restoration-tools/tests
```

Tests generate clean and noisy images with fixed seeds and assert that the
restored image improves MSE or PSNR over the degraded input.
