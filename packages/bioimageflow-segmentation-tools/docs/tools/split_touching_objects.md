# SplitTouchingObjects

`SplitTouchingObjects` applies distance-transform watershed inside each
non-zero label mask and returns a sequentially relabeled output image.

Inputs are `labels` and `min_distance`. Outputs are `output_labels` and
`object_count`.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_segmentation_tools import SplitTouchingObjects

result = SplitTouchingObjects().process_row(
    Arguments(labels="clumped.tif", output_labels="split.tif", min_distance=5)
)
```

## Inputs

- `labels`: 2D label image.
- `min_distance`: minimum marker spacing in pixels.

## Outputs

- `output_labels`: relabeled split objects.
- `object_count`: number of output labels.

## Dependencies and Core Libraries

imageio, NumPy, SciPy distance transforms, and scikit-image watershed helpers.

## Assumptions

Each input label is a clump where distance peaks can approximate object centers.

## Expected Results

Synthetic clumped disks split into separate sequential output labels.

## Failure Modes

Unreadable labels, invalid marker settings, unsupported dimensions, and write
failures raise errors.
