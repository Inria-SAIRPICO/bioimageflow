# FilterSpots

`FilterSpots` filters spot coordinate tables using optional numeric thresholds
and an optional mask image.

## Inputs

- `spots_csv`: table with `y` and `x` columns.
- `min_intensity`, `max_intensity`, `min_score`, `max_score`, `min_radius`,
  and `max_radius`: optional filters applied when matching columns exist.
- `mask_image`: optional nonzero mask checked at each spot coordinate.

## Outputs

- `filtered_spots_csv`.
- `spot_count`.

## Dependencies and Core Libraries

Python CSV handling, imageio for optional masks, and NumPy-compatible coordinate
checks.

## Assumptions

Coordinate columns are pixel-space `y` and `x`. Missing optional filter columns
are ignored, but mask filtering requires valid coordinates.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_spot_tools import FilterSpots

FilterSpots().process_row(
    Arguments(spots_csv="spots.csv", min_score=0.5, filtered_spots_csv="kept.csv")
)
```

## Expected Results

Synthetic tables keep only rows within requested score, intensity, radius, and
mask constraints while preserving table columns.

## Failure Modes

Malformed required coordinates, out-of-bounds mask coordinates, unreadable
files, and CSV write failures raise errors.
