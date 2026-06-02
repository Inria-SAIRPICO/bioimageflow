# LinkObjects

`LinkObjects` links objects between adjacent frames using greedy nearest
neighbor assignment.

Inputs are `objects_csv` and `max_distance`. Output is `tracks_csv`, with
`track_id`, `frame`, `label`, `y`, `x`, and `area`, plus `track_count`.

Use it for simple demonstrations where objects move less than `max_distance`
between consecutive frames and do not cross. It does not model divisions,
missed detections, appearance costs, or global optimization.

## Dependencies and Core Libraries

BioImageFlow core APIs, NumPy for Euclidean distance, and csv.

## Assumptions

The input object CSV has `frame`, `label`, `y`, `x`, and `area` columns and
contains detections sorted or sortable by frame.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import LinkObjects

LinkObjects().process_row(Arguments(objects_csv="objects.csv", max_distance=5.0))
```

## Expected Results

The tracks CSV contains stable `track_id` assignments for adjacent-frame
objects within `max_distance`.

## Failure Modes

Missing columns, malformed numeric values, empty CSV files, or crowded motion
that violates nearest-neighbor assumptions produce errors or biologically
incorrect links.
