# TrackQualityMetrics

`TrackQualityMetrics` computes simple quality metrics for linked track dataframe rows.

The tool is a `DataFrameTool`: pass an upstream track dataframe positionally, then configure `min_track_length`.
The input dataframe must contain `track_id`, `frame`, and `label`.
Outputs are `track_count`, `gap_count`, `split_count`, `merge_count`, and `short_track_fraction`.
No quality CSV artifact is written.

## Dependencies and Core Libraries

BioImageFlow core APIs and package-local numeric helpers.

## Minimal Example

```python
import pandas as pd

from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import TrackQualityMetrics

tracks = pd.DataFrame([
    {"track_id": 1, "frame": 0, "label": 1},
])

quality = TrackQualityMetrics().transform(tracks, Arguments(min_track_length=3))
```

## Expected Results

The output dataframe contains one summary row with quality counters and short-track fraction.

## Failure Modes

Missing required numeric fields or invalid thresholds raise errors.
