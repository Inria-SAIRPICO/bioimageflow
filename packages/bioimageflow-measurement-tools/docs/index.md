# bioimageflow-measurement-tools

`bioimageflow-measurement-tools` provides lightweight feature extraction, object counting, table summarization, and label benchmark metrics.

Image tools are worker-safe and use imageio, NumPy, and scikit-image from the general execution environment.
Table tools run in the orchestrator process with pandas and expose dynamically resolved output columns when the upstream schema and configuration are known.
Every label raster must be 2D, finite, non-negative, and integer-valued, with `0` reserved for background.

## Tools

- [RegionProperties](tools/region_properties.md): area, centroid, and bounding box per label.
- [ShapeProperties](tools/shape_properties.md): standard shape features per label.
- [IntensityProperties](tools/intensity_properties.md): intensity summary per label.
- [CountLabels](tools/count_labels.md): count objects and labeled pixels.
- [SummarizeTable](tools/summarize_table.md): summarize numeric table columns.
- [LabelBenchmark](tools/label_benchmark.md): foreground pixel agreement between predicted and reference labels.
- [ObjectMatchingMetrics](tools/object_matching_metrics.md): greedy object matching from predicted labels to reference labels.
- [DiceIoU](tools/dice_iou.md): binary foreground Dice and IoU for masks or labels.
- [AggregatePerImage](tools/aggregate_per_image.md): per-image summaries from object-level feature tables.
- [NormalizeFeatures](tools/normalize_features.md): z-score, robust, or min-max normalization for feature columns.

## Demo Workflow

- [Object measurement workflow](workflows/object_measurement.md): measure label geometry, intensity, counts, and summary tables from generated fixtures.

## Tests and Demo Data

Run package tests with:

```bash
uv run pytest packages/bioimageflow-measurement-tools/tests
```

The current tests generate labeled objects, invalid label fixtures, intensity images, and small tables with exact expected values.
Future public-data tests should be marked `public_data` and should assert broad metric ranges rather than storing large datasets.
