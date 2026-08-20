# MaskToLabels

`MaskToLabels` maps independently over mask-image rows and creates one connected-component label image per mask.

The required input is `mask_image`.
Outputs are `label_image` and `label_count`.

## Dependencies and Core Libraries

BioImageFlow core APIs, NumPy image allocation, imageio, and package-local connected-component helpers.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_spot_tools import MaskToLabels

MaskToLabels().process_row(Arguments(
    mask_image="spots.tif",
    label_image="spots_labels.tif",
))
```

## Expected Results

The output is a `uint32` label image with background `0` and one sequential non-zero label per connected foreground component.
An empty mask produces an all-zero label image and reports `label_count=0`.

## Failure Modes

Unreadable masks and unwritable output paths raise errors.
Masks with more connected components than `uint32` can represent raise `ValueError`.
