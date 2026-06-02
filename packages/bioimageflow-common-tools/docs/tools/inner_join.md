# InnerJoin

`InnerJoin` merges upstream tables on their row index. It is the default
alignment operation when parallel branches produce row-compatible outputs.

The tool has no inputs. It preserves the first occurrence of duplicate columns,
matching BioImageFlow's default merge behavior. It returns no rows when there
is no shared index between upstream tables.

Use it when branches are known to describe the same row set. Do not use it for
metadata joins where a named key column is required; use `JoinOnColumn`
instead.

## Dependencies and Core Libraries

BioImageFlow DataFrameTool merge APIs and pandas.

## Assumptions

Upstream tables have compatible indexes and duplicate columns should keep the
leftmost value.

## Minimal Example

```python
from bioimageflow_common_tools import InnerJoin

joined = InnerJoin()(left_node, right_node, name="joined")
```

## Expected Results

The output table keeps rows with indexes present in every upstream table.

## Failure Modes

Mismatched indexes can drop rows; duplicate columns after the first upstream
table are not preserved.
