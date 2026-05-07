# BioImageFlow Restoration Tools

Phase 3 tools for image restoration baselines and synthetic benchmarking.

## Tools

- `RestoreImage`: applies a scikit-image restoration baseline when scikit-image is installed. A small NumPy fallback keeps default tests and examples runnable.
- `BenchmarkRestoration`: generates a synthetic clean image, applies blur and noise, restores it, and writes PSNR/MSE metrics.

Install scikit-image separately for evaluation runs that need the full restoration backend.

## Example

See `example-workflows/phase3_restoration_benchmark/workflow.py`.
