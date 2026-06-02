# SpotColocalization

`SpotColocalization` greedily matches reference and query spot tables within a
distance threshold.

## Inputs

- `reference_spots_csv`: first spot table.
- `query_spots_csv`: second spot table.
- `max_distance`: maximum Euclidean distance for a match.

## Outputs

- `matches_csv`: reference spot ID, query spot ID, and distance.
- `matched_count`.

## Dependencies and Core Libraries

Python CSV handling and NumPy Euclidean-distance calculations.

## Assumptions

Both tables use pixel-space `y` and `x` coordinates. Matching is greedy and each
query spot can be used once.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_spot_tools import SpotColocalization

SpotColocalization().process_row(
    Arguments(reference_spots_csv="a.csv", query_spots_csv="b.csv", matches_csv="matches.csv")
)
```

## Expected Results

Synthetic spot pairs within the distance threshold produce deterministic
one-to-one matches.

## Failure Modes

Missing coordinates, unreadable CSV files, invalid distances, and CSV write
failures raise errors.
