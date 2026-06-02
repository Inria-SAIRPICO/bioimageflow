# FilterTableRows

`FilterTableRows` keeps rows that match a deterministic
column/operator/value predicate. It is intended for workflow glue and avoids
string expression evaluation.

## Inputs

- upstream table: pandas DataFrame from a previous table tool.
- `column`: column to compare.
- `operator`: `eq`, `ne`, `gt`, `ge`, `lt`, `le`, `contains`, or `in`.
- `value`: comparison value. The `in` operator accepts a comma-separated value
  list.

## Outputs

The output is a DataFrame containing only rows that satisfy the predicate.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_common_tools import FilterTableRows

filtered = FilterTableRows().transform(
    df,
    Arguments(column="condition", operator="eq", value="treated"),
)
```

## Dependencies and Core Libraries

pandas.

## Assumptions

The predicate is one simple comparison against one column. Numeric comparison
operators require numeric-compatible values.

## Expected Results

The output table preserves upstream columns and keeps only matching rows.

## Failure Modes

Missing columns raise `KeyError`. Unsupported operators raise `ValueError`.
Invalid numeric comparisons raise `ValueError`.
