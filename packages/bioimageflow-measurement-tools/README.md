# bioimageflow-measurement-tools

Optional BioImageFlow package for object measurements, label counting, table summaries, and segmentation benchmark metrics.

Worker-side image tools use imageio, NumPy, and scikit-image through BioImageFlow's general environment.
Main-process table tools use pandas and the `DataFrameTool` API.
The two tool families live in separate modules so worker imports never depend on the orchestrator package.

Label inputs must be finite, non-negative, integer-valued 2D rasters; `0` is background.
Table tools reject empty or duplicate selections, nonnumeric selected values, and output-name collisions.

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

The tests use generated label images, intensity images, and tables with exact expected values.
Public-data workflow tests should assert stable ranges rather than commit large datasets.
