# SpotsToLabels

`SpotsToLabels` creates connected spot label images from a coordinate table or
from a binary mask.

## Inputs

- `spots_csv`: optional coordinate table with `y`, `x`, and optional `spot_id`.
- `mask_image`: optional binary or label-like mask.
- `image_shape`: output shape for coordinate-table rendering.
- `radius`: rendered disk radius for coordinate tables.

## Outputs

- `label_image`.
- `label_count`.

## Dependencies and Core Libraries

Python CSV handling, NumPy image allocation, imageio, and package-local
connected-component traversal.

## Assumptions

Exactly one of `spots_csv` or `mask_image` should define the source spots.
Coordinate tables create one label per row; masks create one label per
connected component.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_spot_tools import SpotsToLabels

SpotsToLabels().process_row(
    Arguments(spots_csv="spots.csv", image_shape="256,256", label_image="labels.tif")
)
```

## Expected Results

Coordinate fixtures render one labeled object per spot; mask fixtures relabel
connected components sequentially.

## Failure Modes

Missing sources, malformed coordinates, invalid shapes, unreadable masks, and
write failures raise errors.
