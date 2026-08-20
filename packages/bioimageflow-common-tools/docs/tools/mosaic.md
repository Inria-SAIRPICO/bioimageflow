# Mosaic

`Mosaic` aggregates row-wise images into a grid montage.
It is a batch tool: all input rows are read, optionally resized, and pasted into one output image.

Inputs are `input_image`, `columns`, `tile_width`, and `tile_height`.
Outputs are `mosaic_path` and `image_count`.
The core library is Pillow.

Use it for quick visual QC of image batches or restored/segmented outputs.
Without an explicit tile size, each grid cell uses the widest and tallest loaded tile, so differently sized images do not overlap.
8-bit and 16-bit grayscale input remains grayscale, RGB input remains RGB, and any alpha-bearing tile produces an RGBA mosaic.
Large batches or very large tiles can create a large in-memory canvas.

## Dependencies and Core Libraries

BioImageFlow core batch-processing APIs and Pillow.

## Assumptions

All input rows must share the same column and resize settings.
An empty batch returns no output rows because there is no row from which to resolve an output path.

## Minimal Example

```python
from bioimageflow_common_tools import Mosaic

mosaic = Mosaic()(input_image=images["path"], columns=5, name="qc_mosaic")
```

## Expected Results

Every input row receives the same `mosaic_path`, and `image_count` equals the number of input images.

## Failure Modes

Unreadable images fail through Pillow.
Invalid or inconsistent layout settings fail before the mosaic is written, and large batches can create large memory allocations.
