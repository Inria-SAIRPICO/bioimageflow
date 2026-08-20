# bioimageflow-tracking-tools

`bioimageflow-tracking-tools` provides deterministic table utilities for live-cell migration tracking.
It extracts object centroids from label movies, links adjacent frames, renders tracks, and computes unambiguous migration summaries.

Core libraries are imageio, NumPy, pandas, SciPy, scikit-image, and BioImageFlow APIs.

## Tools

- [LabelsToObjects](tools/labels_to_objects.md): convert labels into object centroid dataframe rows.
- [NearestNeighborLink](tools/nearest_neighbor_link.md): globally assign objects between adjacent frames by centroid distance.
- [TrackMetrics](tools/track_metrics.md): explicit duration, path length, displacement, speed, and area summaries.
- [FilterObjects](tools/filter_objects.md): filter object dataframe rows by area, frame, intensity, and position.
- [TracksToLabels](tools/tracks_to_labels.md): render track IDs into label stacks.
- [TrackTableValidate](tools/track_table_validate.md): validate required columns, numeric values, ordering, and duplicate assignments.
- [TrackQualityMetrics](tools/track_quality_metrics.md): compute gap counts, duplicate assignment conflicts, and short-track fraction.

## Workflow Use

Use the live-cell migration tracking workflow in the main workflow catalog for a complete labels-to-metrics pipeline.
Lineage and division analysis are outside the scope of these centroid-table tools.
