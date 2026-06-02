# Puncta Analysis Workflow

This workflow demonstrates a compact FISH-like analysis: detect puncta,
associate them with segmented objects, and summarize per-object signal.

## Analysis question

Which puncta are present in an intensity image, which segmented object contains
each punctum, and what per-object signal summary should be reported?

## Data

Use generated 2D intensity images containing three bright puncta and a generated
2D label image with two objects. This allows exact expected counts without
external downloads.

## Expected Results

- `DetectSpots` returns three spots for the default synthetic image.
- `AssignSpotsToLabels` adds a `label` column and counts assigned spots.
- `SpotSummary` writes per-label `spot_count`, `mean_intensity`, and
  `total_intensity`.

## Test coverage

The specialized workflow test executes the generated fixture and verifies that
the summary CSV artifact exists and is non-empty.

```python
from bioimageflow_spot_tools import AssignSpotsToLabels, DetectSpots, SpotSummary
from bioimageflow_core import Arguments

detected = DetectSpots().process_row(
    Arguments(input_image="puncta.tif", threshold=0.3)
)
assigned = AssignSpotsToLabels().process_row(
    Arguments(spots_csv=detected.spots_csv, label_image="labels.tif")
)
SpotSummary().process_row(Arguments(assigned_spots_csv=assigned.assigned_spots_csv))
```
