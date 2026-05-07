# BioImageFlow Tracking Tools

Phase 3 tools for label-stack object extraction and lightweight tracking.

## Tools

- `LabelsToObjects`: converts 2D or TYX label images into object centroid and area tables.
- `LinkObjects`: links objects with a nearest-neighbor frame-to-frame implementation suitable for normal tests.
- `TrackMetrics`: computes track length, displacement, speed, and area summaries.

btrack and LapTrack remain optional heavy integrations and are not normal package dependencies; they are not required for default tests.

## Example

See `example-workflows/phase3_tracking/workflow.py`.
