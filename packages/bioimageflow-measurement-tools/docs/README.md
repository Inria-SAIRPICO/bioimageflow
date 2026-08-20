# BioImageFlow Measurement Tools

Lightweight measurement tools:

- `RegionProperties`: area, centroid, and bounding box per non-zero label.
- `ShapeProperties`: perimeter, extent, aspect ratio, bbox area, and equivalent diameter.
- `IntensityProperties`: intensity statistics per non-zero label.
- `CountLabels`: label and foreground pixel counts.
- `SummarizeTable`: count, mean, min, max, and sum for table columns.
- `LabelBenchmark`: simple foreground pixel agreement metrics.
- `ObjectMatchingMetrics`: greedy predicted/reference object matching by IoU.
- `DiceIoU`: foreground Dice and IoU metrics for binary or label masks.
- `AggregatePerImage`: object-level table aggregation by image/sample.
- `NormalizeFeatures`: z-score, robust, and min-max feature scaling.

Worker-side image tools use imageio, NumPy, and scikit-image from BioImageFlow's general execution environment.
Main-process table tools use pandas and the `DataFrameTool` API.
