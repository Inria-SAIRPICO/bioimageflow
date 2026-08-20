# bioimageflow-common-tools

`bioimageflow-common-tools` provides lightweight workflow glue for BioImageFlow.
It focuses on source tables, table reshaping, simple joins, small image helpers, and report-friendly exports.
Domain-heavy segmentation, restoration, tracking, spot detection, and large-format IO belong in the specialized optional packages.

## Tools

- `Files`: create a source table from an explicit ordered file list or a directory scan.
- `TableFromCsv`: load CSV or TSV metadata into a source table.
- `Generate`: create a source table from literal values.
- `InnerJoin`, `CrossJoin`, `JoinOnColumn`, `Concat`, `Collect`: combine workflow tables.
- `FilterTableRows`: keep rows matching a column/operator/value predicate.
- `SelectColumns`: keep and optionally rename table columns.
- `WriteTable`: persist an upstream table to CSV or TSV.
- `ConnectedComponents`, `LabelOverlaps`, `Mosaic`: small common image and reporting helpers.

## Dependencies

Install-time libraries are BioImageFlow, pandas, imageio, NumPy, and Pillow.
Image-processing tools use the shared `GENERAL_ENV`, which pins imageio, scikit-image, NumPy, and Pillow for worker execution.
New domain-specific tools should live in their own packages instead of expanding this package's scope.

## Migration

`ExtractChannel` has moved out of this package.
Use `SelectChannel` from `bioimageflow-io-tools` for channel selection.

## Tests

```bash
uv run pytest packages/bioimageflow-common-tools/tests
```

Package-owned docs live in `docs/`, with one page per public tool under `docs/tools/`.
