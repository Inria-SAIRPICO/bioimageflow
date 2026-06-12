# LinkObjects

`LinkObjects` links object dataframe rows frame-to-frame with a lightweight nearest-neighbor method.

The tool is a `DataFrameTool`: pass an upstream object dataframe positionally, and configure `max_distance` as a parameter.
The input dataframe must contain `frame`, `label`, `y`, `x`, and `area`.
The output preserves input columns and adds `track_id` and `track_count`.
No tracks CSV artifact is written.

## Dependencies and Core Libraries

BioImageFlow core APIs and NumPy for Euclidean distance.

## Minimal Example

```python
import pandas as pd

from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import LinkObjects

objects = pd.DataFrame([
    {"frame": 0, "label": 1, "y": 5.0, "x": 5.0, "area": 16},
    {"frame": 1, "label": 1, "y": 6.0, "x": 5.0, "area": 16},
])

tracks = LinkObjects().transform(objects, Arguments(max_distance=5.0))
```

## Expected Results

The output dataframe contains stable `track_id` assignments for adjacent-frame objects within `max_distance`.

## Failure Modes

Missing columns, malformed numeric values, empty input rows, or crowded motion can create new tracks rather than links.
