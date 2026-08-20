# SpotSummary

`SpotSummary` aggregates assigned spot rows by label.

`SpotSummary` is a dataframe tool that consumes one upstream assigned spot dataframe.
By default it uses the `label` and `intensity` columns from `AssignSpotsToLabels`; override `label_column` or `intensity_column` when using custom table schemas.
When expanded BioImageFlow indices are available, their lineage roots keep labels from independent source images separate automatically.
Set `group_by` to an explicit image or sample column when the input table does not retain compatible lineage.
Outputs are one dataframe row per source group and non-zero label: `group`, `label`, `spot_count`, `mean_intensity`, `total_intensity`, and `label_count`.
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

The output dataframe contains one row per source group and label with count and intensity summaries.

## Failure Modes

Labels must be non-negative integers and intensities must be finite numbers; label `0` is treated as background and omitted.
Missing or malformed label and intensity columns, invalid `group_by` names, and missing group values raise errors.
