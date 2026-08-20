# DetectSpots

`DetectSpots` detects puncta as local maxima in a scored 2D intensity image.
Supported methods are Difference of Gaussians (`dog`), Laplacian of Gaussian (`log`), and direct `local_maxima`.

Inputs are `input_image`, `method`, `sigma`, `sigma_ratio`, `threshold`, and `min_distance`.
Outputs are `output_labels`, `spot_id`, `y`, `x`, `intensity`, `score`, and `spot_count`.
BioImageFlow records one dataframe row per detected spot, so no separate spot CSV artifact is written.

Use it as a deterministic baseline for puncta workflows. It supports finite 2D scalar images only.
Border maxima are included, an equal-valued plateau contributes its lexicographically first `(y, x)` pixel, and stronger peaks suppress weaker peaks within a Chebyshev distance less than or equal to `min_distance`.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, NumPy, SciPy filtering, and scikit-image local-maxima and plateau labeling.

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

The label image marks one pixel per detected spot, and the workflow output dataframe contains one row per accepted local maximum.
The label image is written as `uint32`; background is `0`, and detected spots use positive sequential IDs.
When no spots pass the threshold, the dataframe is empty and the blank `uint32` label image is still published as the node's `output_labels` artifact.

## Failure Modes

Invalid methods or filter parameters, non-finite or non-2D inputs, and missing files raise errors.
Producing more labels than `uint32` can store raises `ValueError`.
