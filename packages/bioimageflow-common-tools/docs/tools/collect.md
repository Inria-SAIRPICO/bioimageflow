# Collect

`Collect` joins ancestor tables on their index while keeping all columns. When
incoming columns collide, the later duplicate is renamed with a numeric suffix.

It is intended for graph branches where users want a single inspection table
containing inputs, intermediate measurements, and final outputs. It has no
inputs and returns all collected columns.

Failure modes are the usual pandas index-alignment behaviors: rows absent from
one upstream table are dropped by the inner join.

## Dependencies and Core Libraries

BioImageFlow DataFrameTool merge APIs and pandas.

## Assumptions

Ancestor tables are index-compatible and retaining all columns is useful for
inspection.

## Minimal Example

```python
from bioimageflow_common_tools import Collect

collected = Collect()(raw, labels, measurements, name="collected")
```

## Expected Results

The output contains all non-conflicting columns and numeric suffixes for later
duplicate columns.

## Failure Modes

Rows missing from one upstream table are dropped by the inner join; many
duplicate columns can make the inspection table hard to interpret.
