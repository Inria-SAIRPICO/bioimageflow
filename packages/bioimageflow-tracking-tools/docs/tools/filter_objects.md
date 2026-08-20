# FilterObjects

`FilterObjects` filters object dataframe rows by area, frame, intensity, and position.

The tool is a `DataFrameTool`: pass an upstream object dataframe positionally, then configure threshold fields.
Each requested threshold requires its matching dataframe column; without thresholds, any dataframe can pass through.
The output preserves input columns for kept rows and adds `object_count`.
No filtered objects CSV artifact is written.

## Dependencies and Core Libraries

BioImageFlow core APIs and package-local numeric helpers.

## Minimal Example

```python
import pandas as pd

from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import FilterObjects

objects = pd.DataFrame([
    {"frame": 0, "label": 1, "y": 4.0, "x": 4.0, "area": 16},
])

filtered = FilterObjects().transform(objects, Arguments(min_area=10))
```

## Expected Results

Rows inside all requested thresholds pass through as dataframe rows.

## Failure Modes

Missing columns for requested filters, non-numeric data, non-finite bounds, and minimum bounds greater than maximum bounds raise errors.
