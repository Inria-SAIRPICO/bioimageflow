# Tracking Analysis Workflow

This workflow demonstrates a minimal tracking pipeline from label images to track metrics.

## Analysis question

Can segmented objects in a time-lapse label stack be linked into stable tracks and summarized for quality control?

## Data

Use a generated `TYX` label stack with two objects moving across three frames.
The expected output is deterministic: six object rows, two tracks, and mean track length of three.

## Expected Results

- `LabelsToObjects` returns object centroid and area dataframe rows.
- `LinkObjects` assigns two stable track IDs with the default max distance.
- `TrackMetrics` returns one metrics row per track.

## Test coverage

The specialized workflow test executes the generated time-lapse fixture and verifies that the metrics dataframe is non-empty.

```python
from bioimageflow_tracking_tools import LabelsToObjects, LinkObjects, TrackMetrics

objects = LabelsToObjects()(label_image="labels.tif")
tracks = LinkObjects()(
    objects,
    max_distance=8.0,
)
TrackMetrics()(
    tracks,
)
```
