# Restoration Benchmark Workflow

This workflow demonstrates restoration with measurable expected results.
It creates a synthetic clean image, degrades it with blur and noise, restores it, and returns quality metrics in the output dataframe.

## Analysis question

Does a restoration baseline improve a degraded microscopy-like image according to reproducible quality metrics?

## Data

Data are generated from a fixed random seed.
No public downloads are needed for the normal test path.

## Expected Results

- `BenchmarkRestoration` writes clean, degraded, and restored image artifacts.
- `BenchmarkRestoration` returns `mse_degraded`, `mse_restored`, `degraded_psnr`, and `restored_psnr` dataframe columns.
- `restored_psnr` should be greater than `degraded_psnr` for the default generated example.
- `RestoreImage` should write a restored image with the same shape as input.

## Test coverage

The specialized workflow test executes the generated benchmark and verifies that the metrics dataframe is non-empty and the restored image exists.

```python
from bioimageflow_restoration_tools import BenchmarkRestoration
from bioimageflow_core import Arguments

result = BenchmarkRestoration().process_row(
    Arguments(image_size=64, noise_sigma=0.12, blur_sigma=1.0, seed=7)
)
print(result.restored_psnr)
```
