# SpotSummary

`SpotSummary` aggregates assigned spots per non-zero label.

Input is `assigned_spots_csv` from `AssignSpotsToLabels`. Output is
`summary_csv`, with `label`, `spot_count`, `mean_intensity`, and
`total_intensity`, plus `label_count`.

Use it for per-cell or per-nucleus puncta summaries. Background label `0` is
ignored. Empty assignments produce an empty summary table with the expected
columns.

## Dependencies and Core Libraries

BioImageFlow core APIs and csv.

## Assumptions

The input CSV comes from `AssignSpotsToLabels` and contains numeric `label` and
`intensity` columns.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_spot_tools import SpotSummary

SpotSummary().process_row(Arguments(assigned_spots_csv="spots_assigned.csv"))
```

## Expected Results

The summary CSV has one row per non-zero label with spot count, mean intensity,
and total intensity.

## Failure Modes

Missing columns or malformed numeric values fail during CSV parsing or
aggregation.
