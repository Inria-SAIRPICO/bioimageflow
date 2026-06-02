# CountLabels

`CountLabels` reports the number of non-zero labels and the number of
foreground pixels in a 2D label image.

Input is `label_image`. Outputs are `label_count` and `object_pixel_count`.
This is a compact QC tool for segmentation smoke tests and workflow branching.

The tool treats all positive/non-zero pixels as object pixels. Non-2D label
images raise a `ValueError`.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, and NumPy.

## Assumptions

Labels are integer-like 2D arrays and all non-zero pixels are foreground.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_measurement_tools import CountLabels

CountLabels().process_row(Arguments(label_image="labels.tif"))
```

## Expected Results

`label_count` equals the number of distinct non-zero labels, and
`object_pixel_count` equals the number of non-zero pixels.

## Failure Modes

Non-2D labels raise `ValueError`; missing or unsupported files fail through
imageio.
