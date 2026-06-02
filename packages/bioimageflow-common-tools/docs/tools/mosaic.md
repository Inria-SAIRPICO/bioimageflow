# Mosaic

`Mosaic` aggregates row-wise images into a grid montage. It is a batch tool:
all input rows are read, optionally resized, and pasted into one output image.

Inputs are `input_image`, `columns`, `tile_width`, and `tile_height`. Outputs
are `mosaic_path` and `image_count`. The core library is Pillow.

Use it for quick visual QC of image batches or restored/segmented outputs.
Large batches or very large tiles can create a large in-memory canvas. Missing
or unreadable images fail through Pillow.

## Dependencies and Core Libraries

BioImageFlow core batch-processing APIs and Pillow.

## Assumptions

All input rows should share one mosaic output path, and resizing every tile to a
common size is acceptable.

## Minimal Example

```python
from bioimageflow_common_tools import Mosaic

mosaic = Mosaic()(input_image=images["path"], columns=5, name="qc_mosaic")
```

## Expected Results

Every input row receives the same `mosaic_path`, and `image_count` equals the
number of input images.

## Failure Modes

Unreadable images fail through Pillow; large batches can create large memory
allocations.
