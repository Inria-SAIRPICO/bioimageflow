# LabelBenchmark

`LabelBenchmark` compares predicted and reference label images at foreground
pixel level. It reports object counts and simple foreground agreement metrics.

Inputs are `predicted_label_image` and `reference_label_image`. Outputs include
predicted and reference label counts, true-positive, false-positive, and
false-negative pixel counts, and `foreground_iou`.

Use it for small segmentation benchmark demos and smoke tests. It does not
perform instance matching; future measurement backlog tools should add object
matching, Dice, IoU, and panoptic metrics. Shape mismatches raise a `ValueError`.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, and NumPy.

## Assumptions

Both inputs are aligned 2D label images where foreground is `label > 0`.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_measurement_tools import LabelBenchmark

LabelBenchmark().process_row(
    Arguments(predicted_label_image="pred.tif", reference_label_image="gt.tif")
)
```

## Expected Results

The tool returns foreground true-positive, false-positive, false-negative pixel
counts and foreground IoU.

## Failure Modes

Shape mismatches raise `ValueError`; missing or unsupported files fail through
imageio.
