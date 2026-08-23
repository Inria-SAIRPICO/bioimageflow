# Cell Lineage Tracking

The `cell_lineage_tracking` workflow converts a TYX instance-label stack into detections, links them with LapTrack, validates and measures the track table, and renders positive track IDs back into a uint32 label stack.
It supports gap closing and cell divisions while deliberately disabling merges to preserve a one-parent lineage contract.

Run it from the repository root:

```bash
python example_workflows/cell_lineage_tracking/workflow.py --label-image data/labels.tif
```

The canonical output includes one-based track and lineage IDs, nullable parent track IDs, and generation numbers, plus migration and track-quality metrics.
The public-data manifest references LapTrack's C2C12 segmentation example and requires a checksum-pinned crop for complete acceptance testing.
