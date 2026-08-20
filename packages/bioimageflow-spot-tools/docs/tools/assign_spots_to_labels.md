# AssignSpotsToLabels

`AssignSpotsToLabels` assigns each detected spot row to the label value at the same pixel coordinate.

Inputs are `spot_id`, `y`, `x`, `intensity`, optional `score`, and `label_image`.
Outputs are the original spot row columns plus `label` and `assigned_count`.
BioImageFlow records assigned spots in the node dataframe, so no assigned spot CSV artifact is written.

Use it after spot detection and segmentation when per-object spot counts are needed.
Coordinates use nearest-pixel rounding with exact half values rounded upward and must fall inside the label image.

## Dependencies and Core Libraries

BioImageFlow core APIs and imageio for label reading.

## Assumptions

Spot coordinates are in pixel units with `y` as row and `x` as column, matching the label image coordinate system.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_spot_tools import AssignSpotsToLabels

AssignSpotsToLabels().process_row(
    Arguments(spot_id=1, y=12.0, x=9.0, intensity=42.0, label_image="labels.tif")
)
```

## Expected Results

The output row preserves spot columns and adds `label`; `assigned_count` is `1` when the spot lands on a non-zero label and `0` otherwise.

## Failure Modes

Missing, non-finite, or out-of-bounds coordinates; invalid spot IDs; non-finite intensities; malformed label rasters; missing label images; or unreadable files raise errors.
