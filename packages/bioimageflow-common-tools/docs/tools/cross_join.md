# CrossJoin

`CrossJoin` creates the Cartesian product of upstream tables. It is the main
tool for expanding images across parameter grids, models, or repeated
conditions.

The optional `suffixes` input controls duplicate column names for the first
merge pair. Later upstream tables receive numeric suffixes. The output row
count is the product of upstream row counts, so this tool can create very large
tables if used with large inputs.

Expected result: two images crossed with three thresholds produce six rows.
Failure mode: empty upstream input produces an empty table or the remaining
single table, depending on the number of upstream tables.

## Dependencies and Core Libraries

BioImageFlow DataFrameTool merge APIs and pandas.

## Assumptions

Every combination of upstream rows is meaningful and the expanded table remains
small enough to execute.

## Minimal Example

```python
from bioimageflow_common_tools import CrossJoin

grid = CrossJoin()(images, thresholds, name="grid")
```

## Expected Results

Output row count equals the product of upstream row counts.

## Failure Modes

Large inputs can create a combinatorial explosion. Duplicate column names are
suffixed according to the tool settings.
