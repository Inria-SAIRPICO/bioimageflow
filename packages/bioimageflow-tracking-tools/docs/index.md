# bioimageflow-tracking-tools

`bioimageflow-tracking-tools` provides adapters and table utilities for live-cell migration tracking.
It can extract object centroids from label movies, run Ultrack or btrack table adapters, and compute migration summaries.

Core libraries are imageio, NumPy, pandas for dataframe tools, and BioImageFlow core APIs.
Ultrack and btrack are declared in their own runtime environments.

## Tools

- [LabelsToObjects](tools/labels_to_objects.md): convert labels into object centroid dataframe rows.
- [LinkObjects](tools/link_objects.md): small-table nearest-neighbor frame linking.
- [UltrackLink](tools/ultrack_link.md): Ultrack object-table adapter.
- [BTrackLink](tools/btrack_link.md): btrack object-table adapter.
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

## Workflow Use

Use the live-cell migration tracking workflow in the main workflow catalog to compare Ultrack and btrack migration metrics.
Lineage and division analysis are outside the scope of that workflow.
