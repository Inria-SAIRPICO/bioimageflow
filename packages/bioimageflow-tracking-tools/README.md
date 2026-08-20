# bioimageflow-tracking-tools

Tools for label-stack object extraction, deterministic centroid linking, track rendering, and migration metrics.

## Tools

- `LabelsToObjects`: converts 2D or TYX label images into object centroid and area tables.
- `NearestNeighborLink`: links adjacent-frame objects with global one-to-one distance assignment.
- `TrackMetrics`: computes explicit duration, path length, net displacement, speed, and area summaries.
- `FilterObjects`: filters object tables by area, frame, intensity, and position.
- `TracksToLabels`: renders track IDs back into label stacks.
- `TrackTableValidate`: validates required columns, frame order, and duplicate track frames.
- `TrackQualityMetrics`: computes gap counts, duplicate assignment conflicts, and short-track fraction.

The package intentionally does not expose Ultrack or btrack table adapters: those libraries require richer native tracking workflows than a centroid-table compatibility shim can represent truthfully.
