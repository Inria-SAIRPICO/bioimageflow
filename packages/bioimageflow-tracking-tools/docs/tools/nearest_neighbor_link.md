# NearestNeighborLink

`NearestNeighborLink` links centroid rows between adjacent frames with a global one-to-one assignment.

The input dataframe must contain finite `y` and `x` coordinates, non-negative integral `frame` values, and positive integral `label` values.
`area` is not required.
`max_distance` is the inclusive maximum Euclidean distance for a link and defaults to `10.0`.

## Minimal Example

```python
import pandas as pd

from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import NearestNeighborLink

objects = pd.DataFrame([
    {"frame": 0, "label": 1, "y": 4.0, "x": 4.0},
    {"frame": 1, "label": 1, "y": 5.0, "x": 4.0},
])

tracks = NearestNeighborLink().transform(objects, Arguments(max_distance=5.0))
```

## Assignment Semantics

For every pair of adjacent frame numbers, the tool first maximizes the number of links within `max_distance`, then minimizes their total centroid distance.
Each object and track participates in at most one link per frame.
Objects without a valid assignment start new positive sequential track IDs.
A missing frame breaks continuity and starts new tracks.

## Failure Modes

Missing columns, non-finite coordinates, fractional or negative frames, non-positive labels, duplicate `(frame, label)` rows, and invalid `max_distance` values raise `ValueError`.
