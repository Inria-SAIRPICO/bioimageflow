# LabelsToObjects

`LabelsToObjects` converts a 2D label image or a `TYX` label stack into object dataframe rows.

Input is `label_image`.
Outputs are `frame`, `label`, `y`, `x`, `area`, and `object_count`.
No objects CSV artifact is written.

Use it as the bridge from segmentation to tracking.
Label `0` is ignored.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, NumPy, and scikit-image region measurements.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import LabelsToObjects

LabelsToObjects().process_row(Arguments(label_image="labels_tyx.tif"))
```

## Expected Results

The workflow dataframe contains one row per non-zero label per frame with centroid and area columns.
All-background inputs produce an empty object table with the declared columns; no sentinel object row is created.

## Failure Modes

Non-2D/non-3D, non-integer, or negative label rasters raise `ValueError`; missing or unsupported image files fail through imageio.
