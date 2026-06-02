# Concat

`Concat` vertically appends upstream tables. It is useful for collecting rows
from alternative branches that produce compatible result columns.

The tool has no inputs. It returns the union of columns; missing values are
represented by pandas as nulls. If row indexes collide, the result is reset to
a dense integer index to preserve lineage without duplicate index ambiguity.

Use `Concat` for stacking equivalent measurements, not for joining different
measurements for the same objects.

## Dependencies and Core Libraries

BioImageFlow DataFrameTool merge APIs and pandas.

## Assumptions

Upstream tables describe the same kind of rows or can tolerate missing columns
after concatenation.

## Minimal Example

```python
from bioimageflow_common_tools import Concat

all_rows = Concat()(batch_a, batch_b, name="all_rows")
```

## Expected Results

Rows from every upstream table are appended vertically.

## Failure Modes

Column mismatches produce missing values; duplicate indexes are reset.
