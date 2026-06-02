# LabelsToObjects

`LabelsToObjects` converts a 2D label image or a `TYX` label stack into a CSV
of object centroids and areas.

Input is `label_image`. Output is `objects_csv`, with columns `frame`, `label`,
`y`, `x`, and `area`, plus `object_count`.

Use it as the bridge from segmentation to tracking. Non-2D/non-3D inputs raise
`ValueError`, and label `0` is ignored.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, NumPy, and csv.

## Assumptions

The input is either `YX` or `TYX`, labels are integer-like, and each label value
within a frame represents one object.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import LabelsToObjects

LabelsToObjects().process_row(Arguments(label_image="labels_tyx.tif"))
```

## Expected Results

The object CSV contains one row per non-zero label per frame with centroid and
area columns.

## Failure Modes

Non-2D/non-3D inputs raise `ValueError`; missing or unsupported image files fail
through imageio.
