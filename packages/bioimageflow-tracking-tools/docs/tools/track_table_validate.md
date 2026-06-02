# TrackTableValidate

`TrackTableValidate` validates required tracking columns and basic consistency.

## Inputs

- `tracks_csv`: linked track table.

## Outputs

- `validation_csv`: one row per validation error.
- `valid`.
- `error_count`.

## Dependencies and Core Libraries

Python CSV handling and package-local numeric table validation helpers.

## Assumptions

Valid track tables contain `track_id`, `frame`, `label`, `y`, and `x` columns
with non-empty numeric values.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import TrackTableValidate

TrackTableValidate().process_row(
    Arguments(tracks_csv="tracks.csv", validation_csv="validation.csv")
)
```

## Expected Results

Valid synthetic tables return `valid=True` and zero errors. Duplicate
track/frame rows or blank required values are reported in `validation_csv`.

## Failure Modes

Unreadable CSV files and validation CSV write failures raise errors.
