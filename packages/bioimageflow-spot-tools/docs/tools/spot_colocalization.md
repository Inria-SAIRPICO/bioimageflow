# SpotColocalization

`SpotColocalization` is a dataframe tool that globally matches spots from two upstream spot tables with a Euclidean-distance threshold.

Pass the reference spot table as the first positional input and the query spot table as the second positional input.
Both tables must contain `spot_id`, `y`, and `x` columns.
By default, spots are matched independently per BioImageFlow index-lineage root, using the standard `::` row-expansion separator to recover the parent image or field.
Set `group_by` to an image id, image path, or unique-name column when the two tables do not share lineage-compatible indices.

Inputs are `max_distance` and optional `group_by`.
Outputs are match rows with `group`, `reference_spot_id`, `query_spot_id`, `distance`, and `matched_count`.
No matches CSV artifact is written.

## Dependencies and Core Libraries

BioImageFlow dataframe APIs, Pandas, and SciPy global linear assignment.

## Minimal Example

```python
import pandas as pd

from bioimageflow_core import Arguments
from bioimageflow_spot_tools import SpotColocalization

reference = pd.DataFrame(
    {"spot_id": [1], "y": [5.0], "x": [5.0]},
    index=["image_0::0"],
)
query = pd.DataFrame(
    {"spot_id": [7], "y": [6.0], "x": [5.0]},
    index=["image_0::0"],
)

matches = SpotColocalization().merge_dataframes(
    [reference, query],
    Arguments(max_distance=2.0),
)
```

## Expected Results

For each group, matching first maximizes the number of one-to-one pairs within `max_distance`, then minimizes total distance among maximum-cardinality matchings.
The output dataframe contains one row per matched reference/query pair and repeats `matched_count` for each group.
If either input table is empty, the output is an empty table with the declared match columns.

## Failure Modes

Missing spot columns, non-positive/fractional/duplicate per-group spot IDs, non-finite coordinates, negative or non-finite distances, missing groups, or unrelated input groups raise errors.
