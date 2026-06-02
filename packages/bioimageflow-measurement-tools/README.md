# bioimageflow-measurement-tools

Optional BioImageFlow package for object measurements, label counting, table
summaries, and segmentation benchmark metrics.

Core libraries: imageio, NumPy, pandas, and BioImageFlow's `ProcessingTool` and
`DataFrameTool` APIs. The package should grow toward a compact CellProfiler-like
measurement layer while keeping deterministic public tests.

## Tools

- `RegionProperties`
- `ShapeProperties`
- `IntensityProperties`
- `CountLabels`
- `SummarizeTable`
- `LabelBenchmark`
- `ObjectMatchingMetrics`
- `DiceIoU`
- `AggregatePerImage`
- `NormalizeFeatures`

## Tests

Run package tests with:

```bash
uv run pytest packages/bioimageflow-measurement-tools/tests
```

The tests use generated label images, intensity images, and tables with exact
expected values. Public-data workflow tests should assert stable ranges rather
than commit large datasets.
