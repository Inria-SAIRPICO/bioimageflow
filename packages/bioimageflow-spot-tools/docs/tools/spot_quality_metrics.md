# SpotQualityMetrics

`SpotQualityMetrics` computes local spot quality metrics from spot dataframe rows and an intensity image.

`SpotQualityMetrics` is a dataframe tool that consumes one upstream spot dataframe and an `image` parameter.
It requires `y` and `x`, uses `intensity` when present, and otherwise samples the image at each spot coordinate.
Outputs preserve the input rows, including arbitrary extra columns, and add `local_background`, `snr`, `nearest_neighbor_distance`, and `spot_count`.
No metrics CSV artifact is written.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, NumPy annular sampling, and SciPy spatial indexing.

## Minimal Example

```python
import pandas as pd

from bioimageflow_core import Arguments
from bioimageflow_spot_tools import SpotQualityMetrics

spots = pd.DataFrame({"spot_id": [1], "y": [5.0], "x": [5.0], "intensity": [12.0]})
metrics = SpotQualityMetrics().transform(spots, Arguments(image="image.tif"))
```

## Expected Results

Local background and noise are measured in the clipped annulus from `radius` (exclusive) to `2 * radius` (inclusive), excluding every spot's radius-sized footprint.
SNR is `(intensity - local_background) / background_standard_deviation`, with a floating-point epsilon floor for zero-noise backgrounds.
Nearest-neighbor distance uses continuous coordinates and is `0` when the table contains only one spot.

## Failure Modes

Missing, non-finite, or out-of-bounds coordinates, non-positive radii, invalid IDs, non-finite images, empty clipped background annuli, and unreadable images raise errors.
