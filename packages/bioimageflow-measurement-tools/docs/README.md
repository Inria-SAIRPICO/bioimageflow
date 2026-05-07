# BioImageFlow Measurement Tools

Lightweight Phase 1 measurement tools:

- `RegionProperties`: area, centroid, and bounding box per non-zero label.
- `IntensityProperties`: intensity statistics per non-zero label.
- `CountLabels`: label and foreground pixel counts.
- `SummarizeTable`: count, mean, min, max, and sum for table columns.
- `LabelBenchmark`: simple foreground pixel agreement metrics.

The package avoids heavy image-analysis dependencies in Phase 1 and uses
NumPy/Pandas implementations for predictable synthetic-test coverage.
