# bioimageflow-tracking-tools

`bioimageflow-tracking-tools` provides a lightweight label-to-track baseline:
extract object centroids from label movies, link objects frame-to-frame, and
compute track summaries. It is intended for demonstrative workflows and fast
tests before heavier btrack or LapTrack integrations are justified.

Core libraries are imageio, NumPy, pandas for dataframe tools, and BioImageFlow core APIs.
The current linker is greedy nearest-neighbor, so it is transparent and deterministic but not suitable for crowded, crossing, or division-heavy movies.

## Tools

- <a href="tools/labels_to_objects.md">LabelsToObjects</a>: convert labels into object centroid dataframe rows.
- <a href="tools/link_objects.md">LinkObjects</a>: greedy nearest-neighbor frame linking.
- <a href="tools/track_metrics.md">TrackMetrics</a>: track length, displacement, speed, and
  area summaries.
- <a href="tools/filter_objects.md">FilterObjects</a>: filter object dataframe rows by area, frame, intensity, and position.
- <a href="tools/tracks_to_labels.md">TracksToLabels</a>: render track IDs into label stacks.
- <a href="tools/track_table_validate.md">TrackTableValidate</a>: validate required columns and
  duplicate track frames.
- <a href="tools/track_summary.md">TrackSummary</a>: summarize duration, displacement, and speed
  per track.
- <a href="tools/track_quality_metrics.md">TrackQualityMetrics</a>: compute gap counts, split/merge
  flags, and short-track fraction.

## Demo Workflow

- <a href="workflows/tracking_analysis.md">Tracking analysis workflow</a>: convert label movies to object rows, links, and metrics.

## Tests and Demo Data

Run package tests with:

```bash
uv run pytest packages/bioimageflow-tracking-tools/tests
```

Tests generate a three-frame label movie with two objects and assert exact
object counts, track counts, and mean track length.
