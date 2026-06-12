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

## Failure Modes

Missing coordinates, malformed shapes, out-of-bounds coordinates, or unwritable output paths raise errors.
