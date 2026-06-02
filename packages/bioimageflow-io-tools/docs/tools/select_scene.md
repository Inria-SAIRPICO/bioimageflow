# SelectScene

`SelectScene` extracts one scene from an image. Ordinary image files support
scene `0`. TIFF files are read with tifffile when possible, so multi-series
TIFF files can expose additional zero-based scene indexes.

## Inputs

- `input_image`: ordinary image or multi-series TIFF.
- `scene`: zero-based scene index.

## Outputs

- `output_image`: selected scene written as an image file.

## Dependencies and Core Libraries

imageio for ordinary images and tifffile for TIFF series access.

## Assumptions

Only TIFF series expose multiple scenes in the current implementation. Ordinary
images are treated as a single scene at index 0.

Use it when ingestion needs to choose one acquisition series before layout
validation, slicing, or conversion.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_io_tools import SelectScene

SelectScene().process_row(
    Arguments(input_image="multi_series.tif", scene=1, output_image="scene_1.tif")
)
```

## Expected Results

Scene 0 of ordinary images is copied unchanged. Multi-series TIFF inputs write
the selected series array. Out-of-range scenes raise an index error.

## Failure Modes

Negative indexes and indexes outside the available TIFF series raise
`IndexError`. Non-TIFF files only support scene `0`.
