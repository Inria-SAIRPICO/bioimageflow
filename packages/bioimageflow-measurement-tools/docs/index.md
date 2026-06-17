# bioimageflow-measurement-tools

`bioimageflow-measurement-tools` provides lightweight feature extraction,
object counting, table summarization, and label benchmark metrics. It should
grow toward a CellProfiler-like measurement layer: shape, intensity, object QC,
and benchmark features that are useful across segmentation, spot, and tracking
workflows.

Core libraries are imageio, NumPy, pandas, and BioImageFlow's DataFrameTool and
ProcessingTool APIs. The current tools are intentionally deterministic and
small enough to test with generated arrays.

## Tools

- <a href="tools/region_properties.md">RegionProperties</a>: area, centroid, and bounding
  box per label.
- <a href="tools/shape_properties.md">ShapeProperties</a>: deterministic extended shape
  features per label.
- <a href="tools/intensity_properties.md">IntensityProperties</a>: intensity summary per
  label.
- <a href="tools/count_labels.md">CountLabels</a>: count objects and labeled pixels.
- <a href="tools/summarize_table.md">SummarizeTable</a>: summarize numeric table columns.
- <a href="tools/label_benchmark.md">LabelBenchmark</a>: foreground pixel agreement
  between predicted and reference labels.
- <a href="tools/object_matching_metrics.md">ObjectMatchingMetrics</a>: greedy object matching
  from predicted labels to reference labels.
- <a href="tools/dice_iou.md">DiceIoU</a>: binary foreground Dice and IoU for masks or labels.
- <a href="tools/aggregate_per_image.md">AggregatePerImage</a>: per-image summaries from
  object-level feature tables.
- <a href="tools/normalize_features.md">NormalizeFeatures</a>: z-score, robust, or min-max
  normalization for feature columns.

## Demo Workflow

- <a href="workflows/object_measurement.md">Object measurement workflow</a>: measure label
  geometry, intensity, counts, and summary tables from generated fixtures.

## Tests and Demo Data

Run package tests with:

```bash
uv run pytest packages/bioimageflow-measurement-tools/tests
```

The current tests generate two labeled objects, an intensity image, and small
tables with exact expected values. Future public-data tests should be marked
`public_data` and should assert broad metric ranges rather than storing large
datasets.
