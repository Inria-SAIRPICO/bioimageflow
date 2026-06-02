# TrackMetrics

`TrackMetrics` computes one summary row per track.

Input is `tracks_csv` from `LinkObjects`. Output is `metrics_csv`, with
`track_id`, `track_length`, `start_frame`, `end_frame`, `displacement`,
`mean_speed`, and `mean_area`, plus package outputs `track_count` and
`mean_track_length`.

Use it for quick tracking QC and demo assertions. Empty input produces an empty
metrics table and mean track length of `0.0`.

## Dependencies and Core Libraries

BioImageFlow core APIs, NumPy for displacement calculation, and csv.

## Assumptions

The input tracks CSV comes from `LinkObjects` and contains numeric frame,
position, area, and track identifier columns.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import TrackMetrics

TrackMetrics().process_row(Arguments(tracks_csv="tracks.csv"))
```

## Expected Results

The metrics CSV has one row per track with length, start/end frames,
displacement, mean speed, and mean area.

## Failure Modes

Missing columns or malformed numeric values fail during CSV parsing or metric
calculation.
