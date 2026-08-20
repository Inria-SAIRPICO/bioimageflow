# TrackMetrics

`TrackMetrics` computes explicit motion and area measurements from track dataframe rows.

The tool is a `DataFrameTool`: pass an upstream track dataframe positionally.
The input dataframe must contain `track_id`, `frame`, `y`, `x`, and `area`.
Outputs are one row per source stack and track with `source_label_image`, `track_length`, `duration`, `start_frame`, `end_frame`, `path_length`, `net_displacement`, `net_speed`, `mean_step_speed`, `mean_area`, `track_count`, and `mean_track_length`.
No metrics CSV artifact is written.

## Dependencies and Core Libraries

BioImageFlow APIs and NumPy.

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

`duration` is `end_frame - start_frame`; it is zero for a single observation.
`path_length` is the sum of distances between consecutive observations, while `net_displacement` is the first-to-last distance.
`net_speed` is net displacement divided by duration, and `mean_step_speed` is the arithmetic mean of each step distance divided by its frame interval.
Both speeds are zero for a single-observation track.
`mean_area` is the arithmetic mean of all observed areas.
When the input contains `source_label_image`, local track IDs from independent stacks are measured separately and `track_count` and `mean_track_length` are computed per source.

## Failure Modes

Missing columns, non-finite or malformed numeric values, invalid identifiers, and duplicate observations for one track and frame raise errors.
