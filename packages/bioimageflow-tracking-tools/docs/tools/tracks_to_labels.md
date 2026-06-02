# TracksToLabels

`TracksToLabels` renders track IDs into a label stack using the source label
image and a track table with `frame`, `label`, and `track_id` columns.

## Inputs

- `tracks_csv`: linked track table.
- `label_image`: source 2D or TYX label image.

## Outputs

- `output_label_image`: label stack whose pixel values are track IDs.
- `track_count`.

## Dependencies and Core Libraries

Python CSV handling, imageio, NumPy, and package-local numeric table validation
helpers.

## Assumptions

The track table has required `track_id`, `frame`, and `label` fields. Source
labels contain the object labels referenced by the table.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import TracksToLabels

TracksToLabels().process_row(
    Arguments(tracks_csv="tracks.csv", label_image="labels.tif", output_label_image="tracks.tif")
)
```

## Expected Results

Synthetic moving-object fixtures render source object pixels with their track
IDs and report the number of unique tracks.

## Failure Modes

Missing required track fields, unreadable label images, out-of-range frames, and
write failures raise errors.
