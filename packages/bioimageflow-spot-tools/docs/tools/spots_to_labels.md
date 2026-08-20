# SpotsToLabels

`SpotsToLabels` collectively consumes spot dataframe rows and creates one label image.

Inputs are `spot_id`, `y`, `x`, `image_shape`, and `radius`.
Outputs are `label_image` and `label_count`.

## Dependencies and Core Libraries

BioImageFlow core APIs, NumPy image allocation, and imageio.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_spot_tools import SpotsToLabels

SpotsToLabels().process_batch([
    Arguments(spot_id=1, y=12.0, x=9.0, image_shape="256,256", label_image="labels.tif")
])
```

## Expected Results

Every input row receives the same aggregate label-image path and count so the collective tool preserves BioImageFlow batch cardinality.
Coordinates use nearest-pixel rounding with exact half values rounded upward, disks include pixels exactly on their radius, and later rows deterministically overwrite earlier rows where disks overlap.
The label image is written as `uint32`; background is `0`, and non-zero spot IDs are preserved exactly.
`label_count` is the number of distinct positive IDs still visible in the final image, which can be smaller than the number of input rows after complete overlap or duplicate IDs.
When the upstream spot table is empty, coordinate mode still writes a blank `uint32` label image from `image_shape` and reports `label_count=0`.

## Failure Modes

Missing coordinates, malformed shapes, or unwritable output paths raise errors.
Spot-row `spot_id` values must be positive integers no larger than the `uint32` maximum.
