# DistanceWatershedSegment

`DistanceWatershedSegment` thresholds an image into foreground, computes a
distance transform, places markers on local distance peaks, and runs
`skimage.segmentation.watershed`.

Inputs are `input_image`, `threshold`, and `min_distance`. Outputs are `labels`
and `object_count`.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_segmentation_tools import DistanceWatershedSegment

result = DistanceWatershedSegment().process_row(
    Arguments(input_image="mask.tif", labels="labels.tif", threshold=0.5)
)
```

## Inputs

- `input_image`: binary-like or intensity image.
- `threshold`: foreground cutoff.
- `min_distance`: minimum marker spacing in pixels.

## Outputs

- `labels`: watershed label image.
- `object_count`: number of watershed regions.

## Dependencies and Core Libraries

imageio, NumPy, SciPy distance transforms, and scikit-image watershed helpers.

## Assumptions

Foreground objects are brighter/non-zero and have separable distance peaks.
Pixels must be strictly greater than `threshold` to enter the foreground mask.

## Expected Results

Synthetic touching disks split into distinct labels when marker spacing is
appropriate.

## Failure Modes

Unreadable images, invalid marker settings, unsupported dimensions, and write
failures raise errors.
