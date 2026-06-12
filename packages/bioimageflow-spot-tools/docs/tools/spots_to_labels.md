# SpotsToLabels

`SpotsToLabels` creates connected spot labels from spot dataframe rows or from a binary mask image.

Spot-row inputs are `spot_id`, `y`, `x`, `image_shape`, and `radius`.
Mask inputs use `mask_image`.
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

The output label image contains one label per spot row or one label per connected mask component.

## Failure Modes

Missing coordinates, malformed shapes, unreadable masks, or unwritable output paths raise errors.
