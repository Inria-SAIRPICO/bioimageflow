# TracksToLabels

`TracksToLabels` renders track IDs into a label stack using source object labels.

Inputs are `track_id`, `frame`, `label`, and `label_image`.
Outputs are `output_label_image` and `track_count`.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, NumPy, and package-local numeric helpers.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import TracksToLabels

TracksToLabels().process_batch([
    Arguments(track_id=1, frame=0, label=5, label_image="labels.tif", output_label_image="tracks.tif")
])
```

## Expected Results

The output label stack contains track IDs at the pixels occupied by the source labels.
Collective batches containing several source stacks produce one output artifact per source without mixing mappings.
The output is written as `uint32`; background is `0`, and positive track IDs are preserved exactly.
When the upstream track table is empty but `label_image` is provided, `TracksToLabels` still writes an all-background `uint32` label stack matching the source shape and reports `track_count=0`.

## Failure Modes

Missing track fields, invalid label rasters, out-of-bounds frames, absent source labels, duplicate object assignments, multiple objects for one track/frame, inconsistent paths, unreadable images, or unwritable output paths raise errors.
`track_id` and source `label` values must be positive integers no larger than the `uint32` maximum.
