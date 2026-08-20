# ShapeProperties

`ShapeProperties` computes standard scikit-image size and shape descriptors for each positive label in a 2D label image.

Input is `label_image`.
Outputs include `label`, `area`, contour `perimeter`, `bbox_area`, `extent`, `aspect_ratio`, and `equivalent_diameter`.

Use it when segmentation outputs need standard morphology features.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_measurement_tools import ShapeProperties

rows = ShapeProperties().process_row(Arguments(label_image="labels.tif"))
```

## Inputs

- `label_image`: 2D label image with background `0`.

## Outputs

- one DataFrame row per positive label.
- table columns: `label`, `area`, contour `perimeter`, `bbox_area`, `extent`, `aspect_ratio`, and `equivalent_diameter`.

## Dependencies and Core Libraries

imageio, NumPy, and scikit-image region measurements.

## Assumptions

Labels are finite, non-negative integer object IDs and background is zero.

## Expected Results

Synthetic rectangle fixtures produce exact areas, bounding boxes, aspect ratios, standard contour perimeters, and equivalent diameters.

## Failure Modes

Unreadable or invalid label images raise errors.
Empty label images return no object rows.
