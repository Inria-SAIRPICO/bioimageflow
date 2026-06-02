# FilterObjects

`FilterObjects` filters object tables by area, frame, intensity, and position.

## Inputs

- `objects_csv`: table with object rows.
- Optional `min_*` and `max_*` filters for `area`, `frame`, `intensity`, `y`,
  and `x`.

## Outputs

- `filtered_objects_csv`.
- `object_count`.

## Dependencies and Core Libraries

Python CSV handling and package-local numeric table validation helpers.

## Assumptions

Object rows use one row per detected object and numeric columns for selected
filters. Missing optional filter columns are ignored.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import FilterObjects

FilterObjects().process_row(
    Arguments(objects_csv="objects.csv", min_area=20, filtered_objects_csv="kept.csv")
)
```

## Expected Results

Synthetic tables keep only rows within requested area, frame, intensity, and
position ranges.

## Failure Modes

Unreadable CSV files, invalid numeric filter values, and CSV write failures
raise errors.
