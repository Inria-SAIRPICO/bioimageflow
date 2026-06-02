# JoinOnColumn

`JoinOnColumn` joins upstream tables using a shared key column. It wraps
`pandas.DataFrame.merge` for BioImageFlow workflow graphs.

Inputs are `join_column`, `how`, and `suffixes`. `how` may be `inner`, `left`,
`right`, or `outer`. Duplicate non-key columns are suffixed.

Use this tool for metadata, sample, or image identifier joins. It fails with
pandas merge errors when the join column is missing or when incompatible merge
arguments are provided.

## Dependencies and Core Libraries

BioImageFlow DataFrameTool merge APIs and pandas.

## Assumptions

Every upstream table contains the named join column.

## Minimal Example

```python
from bioimageflow_common_tools import JoinOnColumn

joined = JoinOnColumn()(measurements, metadata, join_column="sample_id")
```

## Expected Results

The output contains the configured pandas merge result and one copy of the join
column.

## Failure Modes

Missing join columns, invalid `how` values, or duplicate-key semantics that are
not intended fail or expand rows through pandas merge behavior.
