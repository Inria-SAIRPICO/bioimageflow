# TrackTableValidate

`TrackTableValidate` validates required tracking dataframe columns and basic table consistency.

The tool is a `DataFrameTool`: pass an upstream track dataframe positionally.
The input dataframe must contain `track_id`, `frame`, `label`, `y`, and `x`.
Outputs are validation rows with `severity`, `message`, `valid`, and `error_count`.
No validation CSV artifact is written.

## Dependencies and Core Libraries

BioImageFlow core APIs and package-local numeric table validation helpers.

## Minimal Example

```python
import pandas as pd

from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import TrackTableValidate

tracks = pd.DataFrame([
    {"track_id": 1, "frame": 0, "label": 1, "y": 0.0, "x": 0.0},
])

report = TrackTableValidate().transform(tracks, Arguments())
```

## Expected Results

Valid input returns an informational row; duplicate track/frame rows or blank required values are reported as error rows.

## Failure Modes

Malformed numeric values raise validation errors in the output dataframe.
