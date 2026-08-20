# SummarizeTable

`SummarizeTable` summarizes numeric columns in an upstream table using count, mean, min, max, and sum.

Inputs are optional `group_by` and optional comma-separated `columns`.
Omitting `columns` selects all numeric columns except `group_by`; an explicitly empty selection is invalid.
With `group_by`, the output has one row per group and columns named `<column>_<stat>`.
Without it, the output has one row per selected column and outputs named `column`, `value_count`, `value_mean`, `value_min`, `value_max`, and `value_sum`.

Use it to aggregate object-level measurements into per-image or per-condition tables.
Missing requested columns raise a `ValueError`.

## Dependencies and Core Libraries

BioImageFlow DataFrameTool APIs and pandas.

## Assumptions

Selected values must be numeric or coercible to numbers, and optional `group_by` must name an existing column.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_measurement_tools import SummarizeTable

summary = SummarizeTable().transform(table, Arguments(group_by="sample", columns="area"))
```

## Expected Results

Grouped output columns use `<column>_count`, `<column>_mean`, `<column>_min`, `<column>_max`, and `<column>_sum`.

## Failure Modes

Unknown, empty, duplicate, or nonnumeric selections and grouping/output-name collisions raise `ValueError`.
