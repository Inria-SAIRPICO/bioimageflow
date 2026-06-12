# SpotSummary

`SpotSummary` aggregates assigned spot rows by label.

`SpotSummary` is a dataframe tool that consumes one upstream assigned spot dataframe.
By default it uses the `label` and `intensity` columns from `AssignSpotsToLabels`; override `label_column` or `intensity_column` when using custom table schemas.
Outputs are one dataframe row per non-zero label: `label`, `spot_count`, `mean_intensity`, `total_intensity`, and `label_count`.
No summary CSV artifact is written because BioImageFlow already records the output dataframe.

## Dependencies and Core Libraries

BioImageFlow core APIs.

## Minimal Example

```python
import pandas as pd

from bioimageflow_core import Arguments
from bioimageflow_spot_tools import SpotSummary

assigned = pd.DataFrame({"label": [1, 1], "intensity": [10.0, 12.0]})
summary = SpotSummary().transform(assigned, Arguments())
```

## Expected Results

The output dataframe contains one row per label with count and intensity summaries.

## Failure Modes

Missing or malformed label and intensity columns fail during validation or numeric conversion.
