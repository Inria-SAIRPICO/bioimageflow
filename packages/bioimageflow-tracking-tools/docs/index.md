# bioimageflow-tracking-tools

`bioimageflow-tracking-tools` provides a lightweight label-to-track baseline:
extract object centroids from label movies, link objects frame-to-frame, and
compute track summaries. It is intended for demonstrative workflows and fast
tests before heavier btrack or LapTrack integrations are justified.

Core libraries are imageio, NumPy, pandas for dataframe tools, and BioImageFlow core APIs.
The current linker is greedy nearest-neighbor, so it is transparent and deterministic but not suitable for crowded, crossing, or division-heavy movies.

## Tools

- [LabelsToObjects](tools/labels_to_objects.md): convert labels into object centroid dataframe rows.
- [LinkObjects](tools/link_objects.md): greedy nearest-neighbor frame linking.
- [UltrackLink](tools/ultrack_link.md): Ultrack adapter with deterministic fallback mode.
- [BTrackLink](tools/btrack_link.md): btrack adapter with deterministic fallback mode.
- [TrackMetrics](tools/track_metrics.md): track length, displacement, speed, and
  area summaries.
- [FilterObjects](tools/filter_objects.md): filter object dataframe rows by area, frame, intensity, and position.
- [TracksToLabels](tools/tracks_to_labels.md): render track IDs into label stacks.
- [TrackTableValidate](tools/track_table_validate.md): validate required columns and
  duplicate track frames.
- [TrackSummary](tools/track_summary.md): summarize duration, displacement, and speed
  per track.
- [TrackQualityMetrics](tools/track_quality_metrics.md): compute gap counts, split/merge
  flags, and short-track fraction.

## Demo Workflow

- [Tracking analysis workflow](workflows/tracking_analysis.md): convert label movies to object rows, links, and metrics.

## Tests and Demo Data

Run package tests with:

```bash
uv run pytest packages/bioimageflow-tracking-tools/tests
```

Tests generate a three-frame label movie with two objects and assert exact
object counts, track counts, and mean track length.
