# TrackMetrics

`TrackMetrics` computes track length, displacement, mean speed, and mean area from track dataframe rows.

The tool is a `DataFrameTool`: pass an upstream track dataframe positionally.
The input dataframe must contain `track_id`, `frame`, `y`, `x`, and `area`.
Outputs are one row per track with `track_length`, `start_frame`, `end_frame`, `displacement`, `mean_speed`, `mean_area`, `track_count`, and `mean_track_length`.
No metrics CSV artifact is written.

## Dependencies and Core Libraries

BioImageFlow core APIs and NumPy for displacement calculation.

## Minimal Example

```python
import pandas as pd

from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import TrackMetrics

tracks = pd.DataFrame([
    {"track_id": 1, "frame": 0, "y": 5.0, "x": 5.0, "area": 16},
])

metrics = TrackMetrics().transform(tracks, Arguments())
```

## Expected Results

The output dataframe has one row per track with length, start/end frames, displacement, mean speed, and mean area.

## Failure Modes

Missing columns or malformed numeric values fail during metric calculation.
