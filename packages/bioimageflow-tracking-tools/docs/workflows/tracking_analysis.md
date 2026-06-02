# Tracking Analysis Workflow

This workflow demonstrates a minimal tracking pipeline from label images to
track metrics.

## Analysis question

Can segmented objects in a time-lapse label stack be linked into stable tracks
and summarized for quality control?

## Data

Use a generated `TYX` label stack with two objects moving across three frames.
The expected output is deterministic: six object rows, two tracks, and mean
track length of three.

## Expected Results

- `LabelsToObjects` writes object centroids and areas.
- `LinkObjects` assigns two stable track IDs with the default max distance.
- `TrackMetrics` writes one metrics row per track.

## Test coverage

The specialized workflow test executes the generated time-lapse fixture and
verifies that the metrics CSV artifact exists and is non-empty.

```python
from bioimageflow_tracking_tools import LabelsToObjects, LinkObjects, TrackMetrics
from bioimageflow_core import Arguments

objects = LabelsToObjects().process_row(Arguments(label_image="labels.tif"))
tracks = LinkObjects().process_row(Arguments(objects_csv=objects.objects_csv))
TrackMetrics().process_row(Arguments(tracks_csv=tracks.tracks_csv))
```
