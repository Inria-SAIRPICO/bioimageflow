# AssignSpotsToLabels

`AssignSpotsToLabels` assigns each detected spot to the label value at the
same pixel coordinate.

Inputs are `spots_csv`, which must contain `y` and `x` columns, and
`label_image`, a 2D label image. Output is `assigned_spots_csv`, with an added
`label` column, plus `assigned_count` for spots with labels greater than zero.

Use it after spot detection and segmentation when per-object spot counts are
needed. Coordinates outside the label image are assigned to background label
`0`; malformed CSV files fail through csv parsing or missing keys.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio for label reading, and csv.

## Assumptions

Spot coordinates are in pixel units with `y` as row and `x` as column, matching
the label image coordinate system.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_spot_tools import AssignSpotsToLabels

AssignSpotsToLabels().process_row(
    Arguments(spots_csv="spots.csv", label_image="labels.tif")
)
```

## Expected Results

The assigned CSV preserves spot columns and adds `label`; `assigned_count`
counts rows whose label is greater than zero.

## Failure Modes

Missing CSV columns, malformed coordinates, missing label images, or unreadable
files fail through csv, conversion, or imageio errors.
