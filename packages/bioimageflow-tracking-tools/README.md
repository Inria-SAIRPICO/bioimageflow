# bioimageflow-tracking-tools

Tools for label-stack object extraction, Ultrack and btrack adapters, and migration metrics.

## Tools

- `LabelsToObjects`: converts 2D or TYX label images into object centroid and area tables.
- `LinkObjects`: links small object tables with nearest-neighbor frame-to-frame matching.
- `UltrackLink`: links object tables with Ultrack.
- `BTrackLink`: links object tables with btrack.
- `TrackMetrics`: computes track length, displacement, speed, and area summaries.
- `FilterObjects`: filters object tables by area, frame, intensity, and position.
- `TracksToLabels`: renders track IDs back into label stacks.
- `TrackTableValidate`: validates required columns, frame order, and duplicate track frames.
- `TrackSummary`: reports duration, displacement, speed, and frame bounds per track.
- `TrackQualityMetrics`: computes gap counts, split/merge flags, and short-track fraction.

Use the live-cell migration tracking workflow in the main workflow catalog for a complete Ultrack and btrack comparison.
