# WatershedSegment

`WatershedSegment` segments thresholded foreground using scikit-image watershed
semantics. If marker labels are supplied, foreground pixels are assigned to the
nearest marker. Without markers, connected foreground components are used as
markers.

Inputs are `input_image`, optional `markers_image`, and `threshold`. Outputs are
`labels` and `object_count`. Marker and input shapes must match.

Use it to split touching objects when marker labels are available from another
tool. Failure modes include shape mismatch and unreadable image paths.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, NumPy, and scikit-image watershed/measure
functions.

## Assumptions

The foreground is defined by `input_image >= threshold`, and marker labels, when
provided, are aligned with the input image.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_segmentation_tools import WatershedSegment

WatershedSegment().process_row(
    Arguments(input_image="image.tif", markers_image="markers.tif", threshold=5.0)
)
```

## Expected Results

Foreground pixels are assigned to marker regions, and `object_count` reflects
the number of non-zero labels in the result.

## Failure Modes

Marker/input shape mismatches raise `ValueError`; missing images fail through
imageio; weak markers can under-split or over-split objects.
