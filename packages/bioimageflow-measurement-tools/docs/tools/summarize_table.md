# SummarizeTable

`SummarizeTable` summarizes numeric columns in an upstream table using count,
mean, min, max, and sum.

Inputs are optional `group_by` and optional comma-separated `columns`. With
`group_by`, the output has one row per group. Without it, the output has one
row per summarized column.

Use it to aggregate object-level measurements into per-image or per-condition
tables. Missing requested columns raise a `ValueError`.

## Dependencies and Core Libraries

BioImageFlow DataFrameTool APIs and pandas.

## Assumptions

Requested columns are numeric or can be aggregated by pandas, and optional
`group_by` names an existing grouping column.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_measurement_tools import SummarizeTable

summary = SummarizeTable().transform(table, Arguments(group_by="sample", columns="area"))
```

## Expected Results

Grouped output columns use `<column>_count`, `<column>_mean`, `<column>_min`,
`<column>_max`, and `<column>_sum`.

## Failure Modes

Unknown requested columns raise `ValueError`; non-numeric columns may fail or
produce pandas aggregation errors.
