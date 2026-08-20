# TrackQualityMetrics

`TrackQualityMetrics` computes simple quality metrics for linked track dataframe rows.

The tool is a `DataFrameTool`: pass an upstream track dataframe positionally, then configure `min_track_length`.
The input dataframe must contain `track_id`, `frame`, and `label`.
Outputs are one row per source stack with `source_label_image`, `track_count`, `gap_count`, `duplicate_track_frame_count`, `object_assignment_conflict_count`, and `short_track_fraction`.
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

`gap_count` counts missing frame slots inside tracks.
`duplicate_track_frame_count` counts excess rows assigned to the same track and frame.
`object_assignment_conflict_count` counts excess assignments of the same source `(frame, label)` object.
These are table consistency counters, not biological split or merge events; lineage metrics require explicit lineage data.
When the input contains `source_label_image`, every count and fraction is computed independently for each source stack.

## Failure Modes

Missing required numeric fields or invalid thresholds raise errors.
