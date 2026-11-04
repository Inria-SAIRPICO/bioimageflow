# TracksToLabels

`TracksToLabels` renders track IDs into a label stack using source object labels.

Inputs are `track_id`, `frame`, `label`, and `label_image`.
Outputs are `source_label_image`, `output_label_image`, and `track_count`.
`source_label_image` is a returned reference, while the exact writable `output_label_image` declares `FileGroup.by("label_image")`.
Source grouping uses full admitted paths, so different folders containing equal basenames remain separate owners.
Use `{label_image.stem}_tracks_{group_index}.tif` when stems alone do not distinguish those owners; `{row_index}` can incorrectly split rows contributing to one file.

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
The engine plans those files before scientific work and supplies their keys and member positions through `context.output_files["output_label_image"]`.
The output is written as `uint32`; background is `0`, and positive track IDs are preserved exactly.
Selected source label images are supplied separately as `context.reference_rows`, or as a genuine constant auxiliary `label_image` in `context.batch_arguments`, including when actual track observations are empty.
`TracksToLabels` then writes one all-background `uint32` label stack per selected source, matching its shape and reporting `track_count=0`; reference images never become synthetic track observations.

## Failure Modes

Missing track fields, invalid label rasters, out-of-bounds frames, absent source labels, duplicate object assignments, multiple objects for one track/frame, inconsistent paths, unreadable images, or unwritable output paths raise errors.
`track_id` and source `label` values must be positive integers no larger than the `uint32` maximum.

The lineage workflow joins the rendered table to actual tracks by `source_label_image` using a left join with rendered images retained.
An empty track table therefore preserves the blank image output and nullable track fields without introducing fake track rows.
