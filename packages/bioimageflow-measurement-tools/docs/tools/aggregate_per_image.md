# AggregatePerImage

`AggregatePerImage` turns object-level measurement rows into per-image or
per-sample summaries.

Inputs are `group_by`, `columns`, and `stats`. `columns` and `stats` are
comma-separated strings. The output includes `object_count` plus flattened
`column_stat` fields.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_measurement_tools import AggregatePerImage

summary = AggregatePerImage().transform(
    table,
    Arguments(group_by="image", columns="area,mean_intensity", stats="count,mean,sum"),
)
```

## Inputs

- upstream table: object-level DataFrame.
- `group_by`: column identifying image or sample.
- `columns`: comma-separated numeric columns to summarize.
- `stats`: comma-separated statistics such as `mean`, `sum`, `min`, `max`,
  and `count`.

## Outputs

The output is one row per group with `object_count` and flattened
`column_stat` fields.

## Dependencies and Core Libraries

pandas and BioImageFlow's `DataFrameTool` API.

## Assumptions

The input table fits in memory and selected value columns are numeric.

## Expected Results

Generated fixture tables produce exact per-image aggregate columns with stable
names.

## Failure Modes

Missing columns, non-numeric values in selected columns, invalid statistics, or
unreadable inputs raise errors.
