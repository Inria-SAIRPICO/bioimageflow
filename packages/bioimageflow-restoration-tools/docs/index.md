# bioimageflow-restoration-tools

`bioimageflow-restoration-tools` provides small restoration baselines and a
synthetic benchmark. It is designed for demonstrative workflows that show how
image quality changes can be measured, not as a full production restoration
suite.

Install-time libraries are imageio and NumPy.
`RestoreImage` and `BenchmarkRestoration` use optional scikit-image restoration helpers when available and fall back to package-local NumPy baselines for deterministic tests.
SAIRPICO command-line restoration tools live in `bioimageflow-sairpico-tools`.

## Tools

- [RestoreImage](#restoreimage): apply a TV, bilateral, Gaussian, or
  unsharp-style restoration baseline.
- [BenchmarkRestoration](#benchmarkrestoration): generate a synthetic clean/degraded/restored benchmark and dataframe metrics.
- [GaussianDenoise](#gaussiandenoise): Gaussian smoothing baseline.
- [MedianDenoise](#mediandenoise): median denoising baseline.
- [BackgroundSubtract](#backgroundsubtract): subtract a smoothed background
  estimate.
- [UnsharpMask](#unsharpmask): sharpen by adding high-frequency residuals.
- [RichardsonLucyRestoration](#richardsonlucyrestoration): lightweight
  Richardson-Lucy deconvolution baseline.

## Demo Workflow

- [Restoration benchmark workflow](#restoration-benchmark-workflow):
  generate clean/degraded/restored images and verify metric improvement.

## Tests and Demo Data

Run package tests with:

```bash
uv run pytest packages/bioimageflow-restoration-tools/tests
```

Tests generate clean and noisy images with fixed seeds and assert that the
restored image improves MSE or PSNR over the degraded input.
