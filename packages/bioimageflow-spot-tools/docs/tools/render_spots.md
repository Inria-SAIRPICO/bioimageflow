# RenderSpots

`RenderSpots` renders spot coordinate tables into 2D binary mask or label
images.

## Inputs

- `spots_csv`: table with `y` and `x` columns and optional `spot_id`.
- `image_shape`: output shape as `height,width`.
- `reference_image`: optional image used to derive the output shape.
- `radius`: rendered disk radius in pixels.
- `label_mode`: use spot IDs when true, otherwise write a binary mask.

## Outputs

- `output_image`.
- `spot_count`.

## Dependencies and Core Libraries

Python CSV handling, NumPy image allocation, and imageio output writing.

## Assumptions

Coordinates are 2D pixel centers. If `spot_id` is missing, row order is used for
labels.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_spot_tools import RenderSpots

RenderSpots().process_row(
    Arguments(spots_csv="spots.csv", image_shape="256,256", output_image="spots.tif")
)
```

## Expected Results

Synthetic coordinate fixtures render labeled disks at exact requested pixel
positions.

## Failure Modes

Missing coordinates, coordinates outside the output image, invalid shapes, and
write failures raise errors.
