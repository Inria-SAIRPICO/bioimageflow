# SpotsToLabels

`SpotsToLabels` collectively consumes spot dataframe rows and creates one label image.

Inputs are `spot_id`, `y`, `x`, `image_shape`, and `radius`.
Outputs are `label_image` and `label_count`.

## Dependencies and Core Libraries

BioImageFlow core APIs, NumPy image allocation, imageio, and package-local connected-component helpers.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_spot_tools import SpotsToLabels

SpotsToLabels().process_batch([
    Arguments(spot_id=1, y=12.0, x=9.0, image_shape="256,256", label_image="labels.tif")
])
```

## Expected Results

The output label image contains one label per spot row.
The label image is written as `uint32`; background is `0`, and non-zero spot IDs are preserved exactly.
When the upstream spot table is empty, coordinate mode still writes a blank `uint32` label image from `image_shape` and reports `label_count=0`.

## Failure Modes

Missing coordinates, malformed shapes, or unwritable output paths raise errors.
Spot-row `spot_id` values must be positive integers no larger than the `uint32` maximum.
