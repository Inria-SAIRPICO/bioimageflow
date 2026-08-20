# RegionProperties

`RegionProperties` computes basic geometry for every positive label in a 2D label image using scikit-image region measurements.

Input is `label_image`.
Outputs are `label`, `area`, `centroid_y`, `centroid_x`, and inclusive bounding-box coordinates.
Label `0` is background and is omitted.

Use it after segmentation when object geometry is needed for QC or downstream
joins. Non-2D label images raise a `ValueError`.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, NumPy, and scikit-image `regionprops_table`.

## Assumptions

Labels are finite, non-negative, integer-valued 2D arrays, label `0` is background, and physical pixel sizes are not required.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_measurement_tools import RegionProperties

rows = RegionProperties().process_row(Arguments(label_image="labels.tif"))
```

## Expected Results

One output row is returned for each non-zero label, with exact pixel area,
centroid coordinates, and bounding-box coordinates.

## Failure Modes

Non-2D, non-finite, fractional, or negative labels raise `ValueError`; unreadable files fail through imageio.
