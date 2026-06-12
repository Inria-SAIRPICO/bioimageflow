# FilterSpots

`FilterSpots` filters spot dataframe rows by numeric thresholds and an optional mask image.

`FilterSpots` is a dataframe tool that consumes one upstream spot dataframe.
Thresholds use the conventional `intensity`, `score`, and `radius` columns when the corresponding min/max parameter is set.
When `mask_image` is provided, `y` and `x` are required so each spot can be tested against the nonzero mask.
Outputs preserve the kept input rows, including arbitrary extra columns, and add `spot_count`.
No filtered CSV artifact is written.

## Dependencies and Core Libraries

BioImageFlow core APIs and imageio for optional mask reading.

## Minimal Example

```python
import pandas as pd

from bioimageflow_core import Arguments
from bioimageflow_spot_tools import FilterSpots

spots = pd.DataFrame({"spot_id": [1], "y": [5.0], "x": [7.0], "intensity": [12.0]})
filtered = FilterSpots().transform(
    spots,
    Arguments(min_intensity=5.0),
)
```

## Expected Results

Rows inside all requested thresholds pass through as dataframe rows.

## Failure Modes

Missing columns required by requested filters, malformed coordinates, out-of-bounds mask coordinates, unreadable masks, and invalid numeric thresholds raise errors.
