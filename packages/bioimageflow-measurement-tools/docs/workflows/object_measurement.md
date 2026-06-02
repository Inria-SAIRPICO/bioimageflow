# Object Measurement Workflow

This workflow demonstrates the core value of the measurement package: take a
label image and an aligned intensity image, produce per-object measurements,
and summarize them per sample or image.

## Analysis Question

What are the size, shape, count, and intensity summaries of segmented objects
in a microscopy image, and are those measurements stable enough to aggregate
per sample?

## Data

Use a generated 2D label image with two non-zero labels and an aligned
float-valued intensity image. The expected output is small enough to assert
exact object counts, areas, and intensity means.

## Expected Results

- `RegionProperties` returns one row per non-zero label.
- `IntensityProperties` returns one row per non-zero label and raises if image
  shapes differ.
- `CountLabels` returns object and pixel counts.
- `SummarizeTable` can aggregate numeric outputs by a grouping column.

```python
from bioimageflow_measurement_tools import CountLabels, RegionProperties
from bioimageflow_core import Arguments

regions = RegionProperties().process_row(Arguments(label_image="labels.tif"))
counts = CountLabels().process_row(Arguments(label_image="labels.tif"))
```
