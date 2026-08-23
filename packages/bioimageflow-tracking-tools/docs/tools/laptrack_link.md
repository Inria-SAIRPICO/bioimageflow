# LapTrackLink

`LapTrackLink` consumes canonical object rows containing source label image, frame, label, Y/X centroid, and optional area.
It partitions source stacks, sorts rows deterministically, calls `laptrack==0.17.1`, and restores input order.

Distances are configured in pixels and converted internally to LapTrack's squared Euclidean costs.
Adjacent linking is required, while gap closing and divisions are independently enabled by providing their distance values.
Merges are always disabled.

Outputs preserve canonical object fields and add positive `track_id`, positive `lineage_id`, nullable `parent_track_id`, zero-based `generation`, `track_count`, and `division_count`.
Identifiers are normalized independently per source stack and remain compatible with `TracksToLabels`, where zero is background.
