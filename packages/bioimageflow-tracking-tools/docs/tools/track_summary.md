# TrackSummary

`TrackSummary` computes per-track duration, displacement, speed, and frame bounds from track dataframe rows.

The tool is a `DataFrameTool`: pass an upstream track dataframe positionally.
The input dataframe must contain `track_id`, `frame`, `y`, and `x`.
Outputs are one row per track with `track_length`, `duration`, `start_frame`, `end_frame`, `displacement`, `mean_speed`, and `track_count`.
No summary CSV artifact is written.

## Dependencies and Core Libraries

BioImageFlow core APIs, NumPy distance calculations, and package-local numeric helpers.

## Minimal Example

```python
import pandas as pd

from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import TrackSummary

tracks = pd.DataFrame([
    {"track_id": 1, "frame": 0, "y": 0.0, "x": 0.0},
    {"track_id": 1, "frame": 1, "y": 0.0, "x": 2.0},
])

summary = TrackSummary().transform(tracks, Arguments())
```

## Expected Results

The output dataframe has one row per track with duration and displacement metrics.

## Failure Modes

Missing required numeric fields and malformed numeric values raise errors.
