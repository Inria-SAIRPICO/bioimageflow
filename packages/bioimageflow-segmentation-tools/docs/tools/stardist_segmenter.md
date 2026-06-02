# StarDistSegmenter

`StarDistSegmenter` wraps StarDist 2D pretrained models for star-convex nuclei
or cell-like objects. It supports fluorescence models and an H&E RGB model.

Inputs include `input_image`, `model_name`, optional `channel`, probability and
NMS thresholds, and normalization percentiles. Outputs are `mask` and
`object_count`. Core dependencies are TensorFlow, StarDist, imageio, NumPy, and
tifffile in an isolated environment.

Use it for 2D nuclear segmentation when StarDist's pretrained assumptions match
the data. Runtime failures include unavailable TensorFlow/StarDist, invalid
model names, unsupported image shapes, and channel indexes outside the input
array.

## Dependencies and Core Libraries

BioImageFlow core APIs, TensorFlow, StarDist 0.9.2, csbdeep normalization,
imageio, NumPy, and tifffile in the isolated StarDist environment.

## Assumptions

Objects are reasonably star-convex and match the selected pretrained model's
domain, such as fluorescence nuclei or H&E nuclei.

## Minimal Example

```python
from bioimageflow_segmentation_tools import StarDistSegmenter

stardist = StarDistSegmenter()(
    input_image="nuclei.tif",
    model_name="2D_versatile_fluo",
)
```

## Expected Results

At runtime with StarDist installed, `mask` is a 2D label image and
`object_count` equals the number of detected non-background objects.

## Failure Modes

Missing TensorFlow/StarDist dependencies, invalid image shapes, wrong channel
indexes, model download/cache failures, or poor normalization percentiles can
fail execution or produce poor labels.
