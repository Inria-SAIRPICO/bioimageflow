# LabelBenchmark

`LabelBenchmark` compares predicted and reference label images at foreground pixel level.
It reports object counts and foreground confusion counts plus IoU.

Inputs are `predicted_label_image` and `reference_label_image`.
Outputs include predicted and reference label counts, true-positive, false-positive, and false-negative pixel counts, and `foreground_iou`.

Use it for segmentation benchmark demos and smoke tests.
It does not perform instance matching; use `ObjectMatchingMetrics` for greedy instance matching or `DiceIoU` when foreground Dice is needed.

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

The tool returns foreground true-positive, false-positive, false-negative pixel counts and foreground IoU.

## Failure Modes

Shape mismatches and invalid label rasters raise `ValueError`; missing or unsupported files fail through imageio.
