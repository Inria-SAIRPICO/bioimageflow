# IntensityProperties

`IntensityProperties` computes intensity statistics inside each non-zero label.

Inputs are `label_image` and `intensity_image`. Outputs are `label`,
`mean_intensity`, `min_intensity`, `max_intensity`, and `sum_intensity`.

Use it for per-cell, per-nucleus, or per-spot intensity measurements when the
label and intensity images are already aligned. Shape mismatches raise a
`ValueError`; label `0` is ignored.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, NumPy, and Python float conversion for table
outputs.

## Assumptions

The label and intensity images are aligned, have identical shape, and represent
one 2D plane.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_measurement_tools import IntensityProperties

IntensityProperties().process_row(
    Arguments(label_image="labels.tif", intensity_image="channel.tif")
)
```

## Expected Results

One row per non-zero label with mean, min, max, and summed intensity.

## Failure Modes

Shape mismatches and non-2D labels raise `ValueError`; missing images fail
through imageio.
