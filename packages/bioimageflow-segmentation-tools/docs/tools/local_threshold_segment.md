# LocalThresholdSegment

`LocalThresholdSegment` computes a Sauvola adaptive threshold with
`skimage.filters.threshold_sauvola`, applies an optional offset, and labels
connected foreground components.
Components use face connectivity, and `above=True` selects values strictly greater than the local threshold.

Inputs are `input_image`, `block_size`, `k`, `offset`, and `above`. `block_size`
must be an odd integer greater than or equal to 3.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_segmentation_tools import LocalThresholdSegment

result = LocalThresholdSegment().process_row(
    Arguments(input_image="image.tif", labels="labels.tif", block_size=15, k=0.2)
)
```

## Inputs

- `input_image`: 2D or 3D intensity image.
- `block_size`, `k`, and `offset`: local threshold parameters.
- `above`: when true, pixels above the local threshold are foreground.

## Outputs

- `labels`: connected-component label image.
- `object_count`: number of foreground components.

## Dependencies and Core Libraries

imageio, NumPy, and scikit-image Sauvola thresholding/labeling functions.

## Assumptions

The image is 2D or 3D and local contrast separates foreground from background.

## Expected Results

Synthetic fixtures with local bright objects produce stable component counts
under the documented parameters.

## Failure Modes

Invalid block sizes, unreadable images, unsupported dimensions, and write
failures raise errors.
