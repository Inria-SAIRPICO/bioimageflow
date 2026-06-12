# Puncta Analysis Workflow

This workflow demonstrates a compact FISH-like analysis: detect puncta, associate them with segmented objects, and summarize per-object signal.

## Analysis question

Which puncta are present in an intensity image, which segmented object contains each punctum, and what per-object signal summary should be reported?

## Data

Use generated 2D intensity images containing three bright puncta and a generated 2D label image with two objects.
This allows exact expected counts without external downloads.

## Expected Results

- `DetectSpots` returns one dataframe row per spot plus a label image artifact.
- `AssignSpotsToLabels` adds a `label` column and counts assigned spots.
- `SpotSummary` returns per-label `spot_count`, `mean_intensity`, and `total_intensity` dataframe columns.

## Test coverage

The specialized workflow test executes the generated fixture and verifies that the summary dataframe is non-empty.

```python
from bioimageflow_spot_tools import AssignSpotsToLabels, DetectSpots, SpotSummary

detected = DetectSpots()(input_image="puncta.tif", threshold=0.3)
assigned = AssignSpotsToLabels()(
    spot_id=detected["spot_id"],
    y=detected["y"],
    x=detected["x"],
    intensity=detected["intensity"],
    score=detected["score"],
    label_image="labels.tif",
)
SpotSummary()(assigned)
```
