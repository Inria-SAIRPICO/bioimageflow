# NormalizeFeatures

`NormalizeFeatures` appends scaled versions of numeric feature columns.

Inputs are `columns`, `method`, and `suffix`. Supported methods are `zscore`,
`robust`, and `minmax`. Constant columns are normalized to `0.0` to keep output
deterministic.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_measurement_tools import NormalizeFeatures

normalized = NormalizeFeatures().transform(
    table,
    Arguments(columns="area,score", method="minmax"),
)
```

## Inputs

- upstream table: feature DataFrame.
- `columns`: comma-separated numeric columns to normalize.
- `method`: `zscore`, `robust`, or `minmax`.
- `suffix`: suffix for appended normalized columns.

## Outputs

The output is the upstream table plus normalized feature columns.

## Dependencies and Core Libraries

pandas, NumPy-compatible numeric operations, and BioImageFlow's `DataFrameTool`
API.

## Assumptions

Selected columns are numeric and the table fits in memory.

## Expected Results

Synthetic feature tables produce deterministic scaled values and retain row
order.

## Failure Modes

Missing columns, unsupported methods, unreadable inputs, and invalid numeric
values raise errors.
