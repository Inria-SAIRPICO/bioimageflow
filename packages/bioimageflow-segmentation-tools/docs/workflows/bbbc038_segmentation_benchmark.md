# BBBC038 Segmentation Benchmark Workflow

This workflow demonstrates the priority segmentation story: produce label
images from microscopy inputs, postprocess them, and evaluate them with
measurement tools.

## Goal

Can a segmentation workflow produce nuclei labels that agree with a reference
mask closely enough for benchmark reporting?

## Data

For CI, use generated images with two bright synthetic objects and optional
marker labels. For public validation, use a small BBBC038 subset behind the
`public_data` marker with documented checksums and expected object-count or IoU
ranges.

## Results

- Predicted label images from Cellpose v3, Cellpose-SAM, StarDist, and a classical threshold method.
- Overlays for visual review.
- Benchmark rows with object counts and foreground overlap metrics.

## Validation

The priority workflow test executes the generated fixture, then asserts stable
label counts and foreground IoU. Public BBBC038 downloads remain a slow
extension, not part of the default test path.

```python
from bioimageflow import Workflow
from bioimageflow_segmentation_tools import PostprocessLabels, ThresholdSegment

with Workflow(storage_path="results") as wf:
    labels = ThresholdSegment()(
        input_image="input.tif",
        threshold=5.0,
        name="threshold",
    )
    cleaned = PostprocessLabels()(
        labels=labels["labels"],
        min_size=16,
        name="cleaned",
    )
    wf.compute(cleaned)
```
