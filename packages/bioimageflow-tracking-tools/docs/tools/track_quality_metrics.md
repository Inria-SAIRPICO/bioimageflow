# TrackQualityMetrics

`TrackQualityMetrics` computes table-level quality metrics for linked tracks.

## Inputs

- `tracks_csv`: linked track table.
- `min_track_length`: threshold used for short-track fraction.

## Outputs

- `quality_csv`: track count, gap count, split count, merge count, and
  short-track fraction.
- Scalar outputs mirroring those CSV values.

## Dependencies and Core Libraries

Python CSV handling and package-local numeric table validation helpers.

## Assumptions

Track rows have numeric `track_id`, `frame`, and `label` fields. Split and
merge counts are table-level consistency indicators, not lineage validation.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import TrackQualityMetrics

TrackQualityMetrics().process_row(
    Arguments(tracks_csv="tracks.csv", min_track_length=3, quality_csv="quality.csv")
)
```

## Expected Results

Synthetic tracks with frame gaps and short tracks produce exact gap counts and
short-track fractions.

## Failure Modes

Missing required numeric fields, unreadable CSV files, invalid thresholds, and
CSV write failures raise errors.
