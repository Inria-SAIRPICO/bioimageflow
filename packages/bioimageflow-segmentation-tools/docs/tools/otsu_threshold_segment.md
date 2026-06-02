# OtsuThresholdSegment

`OtsuThresholdSegment` computes a global threshold with
`skimage.filters.threshold_otsu`, creates a foreground mask, and labels
connected components.

Inputs are `input_image` and `above`. Outputs are `labels`, `object_count`, and
the computed `threshold`.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_segmentation_tools import OtsuThresholdSegment

result = OtsuThresholdSegment().process_row(
    Arguments(input_image="image.tif", labels="labels.tif", above=True)
)
```

## Inputs

- `input_image`: 2D intensity image.
- `above`: when true, pixels above the Otsu threshold are foreground.

## Outputs

- `labels`: connected-component label image.
- `object_count`: number of foreground components.
- `threshold`: computed Otsu threshold.

## Dependencies and Core Libraries

imageio, NumPy, and scikit-image thresholding/labeling functions.

## Assumptions

The image has a bimodal enough intensity distribution for global Otsu threshold
to be meaningful. Background is label `0`.

## Expected Results

Synthetic bright objects on a darker background produce the expected number of
connected labels and an Otsu threshold matching `skimage`.

## Failure Modes

Unreadable images, unsupported dimensions, and write failures stop execution.
