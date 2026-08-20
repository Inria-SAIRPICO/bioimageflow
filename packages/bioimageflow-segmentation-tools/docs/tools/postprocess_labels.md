# PostprocessLabels

`PostprocessLabels` removes labels smaller than `min_size` and relabels the
remaining objects sequentially from `1`.

Inputs are `labels` and `min_size`. Outputs are `output_labels` and
`object_count`. The tool preserves image shape and writes a UInt32-style label
array through imageio.

Use it after any segmentation tool when small artifacts should be removed or
label IDs need to be compact. Label `0` is always background.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, and NumPy.

## Assumptions

Input labels are finite, integral, non-negative arrays, label `0` is background, and size is measured in pixels or voxels.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_segmentation_tools import PostprocessLabels

PostprocessLabels().process_row(Arguments(labels="labels.tif", min_size=16))
```

## Expected Results

Objects below `min_size` are removed and remaining object IDs are relabeled
sequentially.

## Failure Modes

Missing or unsupported label images fail through imageio. Very high `min_size`
can remove all labels.
