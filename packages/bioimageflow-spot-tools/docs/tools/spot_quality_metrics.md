# SpotQualityMetrics

`SpotQualityMetrics` computes local spot quality metrics from spot dataframe rows and an intensity image.

`SpotQualityMetrics` is a dataframe tool that consumes one upstream spot dataframe and an `image` parameter.
It requires `y` and `x`, uses `intensity` when present, and otherwise samples the image at each spot coordinate.
Outputs preserve the input rows, including arbitrary extra columns, and add `local_background`, `snr`, `nearest_neighbor_distance`, and `spot_count`.
No metrics CSV artifact is written.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, and NumPy local-window and distance calculations.

## Minimal Example

```python
import pandas as pd

from bioimageflow_core import Arguments
from bioimageflow_spot_tools import SpotQualityMetrics

spots = pd.DataFrame({"spot_id": [1], "y": [5.0], "x": [5.0], "intensity": [12.0]})
metrics = SpotQualityMetrics().transform(spots, Arguments(image="image.tif"))
```

## Expected Results

The output dataframe contains SNR and nearest-neighbor distance values for each spot.

## Failure Modes

Missing coordinates, out-of-bounds coordinates, unreadable images, and malformed numeric values raise errors.
