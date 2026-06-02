# DetectSpots

`DetectSpots` detects puncta as local maxima in a scored 2D intensity image.
Supported methods are Difference of Gaussians (`dog`), Laplacian of Gaussian
(`log`), and direct `local_maxima`.

Inputs are `input_image`, `method`, `sigma`, `sigma_ratio`, `threshold`, and
`min_distance`. Outputs are `output_labels`, `spots_csv`, and `spot_count`.
The CSV columns are `spot_id`, `y`, `x`, `intensity`, and `score`.

Use it as a deterministic baseline for puncta workflows. It currently supports
2D scalar images only. Invalid methods, non-2D images, missing files, or overly
high thresholds are expected failure or empty-result modes.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, NumPy, csv, and optionally SciPy for
`gaussian_laplace`.

## Assumptions

Puncta are local maxima in a 2D scalar image, and `threshold` is expressed in
the filtered score image units.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_spot_tools import DetectSpots

DetectSpots().process_row(
    Arguments(input_image="puncta.tif", method="dog", threshold=0.3)
)
```

## Expected Results

The label image marks one pixel per detected spot, and the CSV contains one row
per accepted local maximum.

## Failure Modes

Invalid methods raise `ValueError`; non-2D inputs raise `ValueError`; missing
files fail through imageio.
