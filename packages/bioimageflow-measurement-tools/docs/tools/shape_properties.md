# ShapeProperties

`ShapeProperties` computes deterministic size and shape descriptors for each
non-zero label in a 2D label image.

Inputs are `label_image`. Outputs include `label`, `area`, pixel-edge
`perimeter`, `bbox_area`, `extent`, `aspect_ratio`, and
`equivalent_diameter`.

Use it when segmentation outputs need lightweight morphology features without
adding heavier measurement dependencies.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_measurement_tools import ShapeProperties

rows = ShapeProperties().process_row(Arguments(label_image="labels.tif"))
```

## Inputs

- `label_image`: 2D label image with background `0`.

## Outputs

- `properties_csv`: one row per non-zero label.
- `object_count`: number of measured objects.
- table columns: `label`, `area`, pixel-edge `perimeter`, `bbox_area`,
  `extent`, `aspect_ratio`, and `equivalent_diameter`.

## Dependencies and Core Libraries

imageio, NumPy, pandas, and deterministic package-local geometry helpers.

## Assumptions

Labels are integer object IDs and background is zero. The current implementation
is intentionally deterministic and 2D-oriented.

## Expected Results

Synthetic rectangle fixtures produce exact areas, bounding boxes, aspect ratios,
and equivalent diameters.

## Failure Modes

Unreadable images or unsupported dimensions fail through the image reader or
measurement code. Empty label images return an empty table and zero objects.
