# CountLabels

`CountLabels` reports the number of distinct positive labels and the number of foreground pixels in a 2D label image.

Input is `label_image`. Outputs are `label_count` and `object_pixel_count`.
This is a compact QC tool for segmentation smoke tests and workflow branching.

The tool treats all positive pixels as object pixels.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, and NumPy.

## Assumptions

Labels are finite, non-negative, integer-valued 2D arrays and `0` is background.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_measurement_tools import CountLabels

CountLabels().process_row(Arguments(label_image="labels.tif"))
```

## Expected Results

`label_count` equals the number of distinct positive IDs, and `object_pixel_count` equals the number of positive pixels.

## Failure Modes

Non-2D, non-finite, fractional, or negative labels raise `ValueError`; missing or unsupported files fail through imageio.
