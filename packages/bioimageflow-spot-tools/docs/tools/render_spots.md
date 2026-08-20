# RenderSpots

`RenderSpots` renders spot dataframe rows into a 2D binary or label image.

Inputs are `spot_id`, `y`, `x`, `image_shape` or `reference_image`, `radius`, and `label_mode`.
Output is `output_image` plus `spot_count`.

## Dependencies and Core Libraries

BioImageFlow core APIs, NumPy image allocation, and imageio output writing.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_spot_tools import RenderSpots

RenderSpots().process_batch([
    Arguments(spot_id=1, y=12.0, x=9.0, image_shape="256,256", output_image="spots.tif")
])
```

## Expected Results

The output image marks each spot coordinate.
Every input row receives the same aggregate image path and count so the collective tool preserves BioImageFlow batch cardinality.
When `reference_image` is connected, rows are grouped by reference image and rendered independently.
Use an output template containing `{reference_image.stem}` when a batch contains multiple reference images so that groups cannot overwrite one another.
Coordinates use nearest-pixel rounding with exact half values rounded upward, disks include pixels exactly on their radius, and later rows overwrite earlier rows where disks overlap.
With `label_mode=True`, the output is a `uint32` label image that preserves positive `spot_id` values exactly and reserves `0` for background.
In label mode, `spot_count` is the number of distinct positive labels visible in the final image.
With `label_mode=False`, the output is a `uint8` binary mask with values `{0, 1}`.
When the upstream spot table is empty, `RenderSpots` still writes a blank image from `image_shape` or `reference_image` and reports `spot_count=0`.

## Failure Modes

Missing coordinates, malformed shapes, out-of-bounds coordinates, conflicting per-group settings or output paths, or unwritable output paths raise errors.
In label mode, `spot_id` must be a positive integer no larger than the `uint32` maximum.
