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

- [RegionProperties](#regionproperties): area, centroid, and bounding
  box per label.
- [ShapeProperties](#shapeproperties): deterministic extended shape
  features per label.
- [IntensityProperties](#intensityproperties): intensity summary per
  label.
- [CountLabels](#countlabels): count objects and labeled pixels.
- [SummarizeTable](#summarizetable): summarize numeric table columns.
- [LabelBenchmark](#labelbenchmark): foreground pixel agreement
  between predicted and reference labels.
- [ObjectMatchingMetrics](#objectmatchingmetrics): greedy object matching
  from predicted labels to reference labels.
- [DiceIoU](#diceiou): binary foreground Dice and IoU for masks or labels.
- [AggregatePerImage](#aggregateperimage): per-image summaries from
  object-level feature tables.
- [NormalizeFeatures](#normalizefeatures): z-score, robust, or min-max
  normalization for feature columns.

## Demo Workflow

- [Object measurement workflow](#object-measurement-workflow): measure label
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
