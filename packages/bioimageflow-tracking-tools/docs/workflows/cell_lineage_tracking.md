# Cell Lineage Tracking

The workflow converts a TYX label stack to object centroids, links them with LapTrack gap closing and divisions, validates and measures the tracks, computes QC, and renders positive track IDs back into a uint32 label stack.

The public-data manifest references LapTrack's C2C12 segmentation example and requires a checksum-pinned crop for complete acceptance testing.
