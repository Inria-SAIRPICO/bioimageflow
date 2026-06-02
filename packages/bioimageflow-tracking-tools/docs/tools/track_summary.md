# TrackSummary

`TrackSummary` computes per-track duration, displacement, speed, and frame
bounds.

## Inputs

- `tracks_csv`: linked track table.

## Outputs

- `summary_csv`: one row per track.
- `track_count`.

## Dependencies and Core Libraries

Python CSV handling, NumPy distance calculations, and package-local numeric
table validation helpers.

## Assumptions

The table contains numeric `track_id`, `frame`, `y`, and `x` fields. Rows are
sorted by frame within each track before summary metrics are computed.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import TrackSummary

TrackSummary().process_row(
    Arguments(tracks_csv="tracks.csv", summary_csv="track_summary.csv")
)
```

## Expected Results

Synthetic tracks produce exact duration, displacement, speed, start-frame, and
end-frame values.

## Failure Modes

Missing required numeric fields, unreadable CSV files, and CSV write failures
raise errors.
