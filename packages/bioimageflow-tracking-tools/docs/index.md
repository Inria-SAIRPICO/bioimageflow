# bioimageflow-tracking-tools

`bioimageflow-tracking-tools` provides a lightweight label-to-track baseline:
extract object centroids from label movies, link objects frame-to-frame, and
compute track summaries. It is intended for demonstrative workflows and fast
tests before heavier btrack or LapTrack integrations are justified.

Core libraries are imageio, NumPy, pandas for dataframe tools, and BioImageFlow core APIs.
The current linker is greedy nearest-neighbor, so it is transparent and deterministic but not suitable for crowded, crossing, or division-heavy movies.

## Tools

- [LabelsToObjects](#labelstoobjects): convert labels into object centroid dataframe rows.
- [LinkObjects](#linkobjects): greedy nearest-neighbor frame linking.
- [TrackMetrics](#trackmetrics): track length, displacement, speed, and
  area summaries.
- [FilterObjects](#filterobjects): filter object dataframe rows by area, frame, intensity, and position.
- [TracksToLabels](#trackstolabels): render track IDs into label stacks.
- [TrackTableValidate](#tracktablevalidate): validate required columns and
  duplicate track frames.
- [TrackSummary](#tracksummary): summarize duration, displacement, and speed
  per track.
- [TrackQualityMetrics](#trackqualitymetrics): compute gap counts, split/merge
  flags, and short-track fraction.

## Demo Workflow

- [Tracking analysis workflow](#tracking-analysis-workflow): convert label movies to object rows, links, and metrics.

## Tests and Demo Data

Run package tests with:

```bash
uv run pytest packages/bioimageflow-tracking-tools/tests
```

Tests generate a three-frame label movie with two objects and assert exact
object counts, track counts, and mean track length.
