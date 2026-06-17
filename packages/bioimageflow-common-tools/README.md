# bioimageflow-common-tools

`bioimageflow-common-tools` provides lightweight workflow glue for
BioImageFlow. It focuses on source tables, table reshaping, simple joins,
small image helpers, and report-friendly exports. Domain-heavy segmentation,
restoration, tracking, spot detection, and large-format IO belong in the
specialized optional packages.

## Tools

- `Files`: create a source table from files in one directory.
- `TableFromCsv`: load CSV or TSV metadata into a source table.
- `Generate`: create a source table from literal values.
- `InnerJoin`, `CrossJoin`, `JoinOnColumn`, `Concat`, `Collect`: combine
  workflow tables.
- `FilterTableRows`: keep rows matching a column/operator/value predicate.
- `SelectColumns`: keep and optionally rename table columns.
- `WriteTable`: persist an upstream table to CSV or TSV.
- `ExtractChannel`, `ConnectedComponents`, `LabelOverlaps`, `Mosaic`: small
  common image and reporting helpers.

## Dependencies

Install-time libraries are BioImageFlow, pandas, imageio, NumPy, and Pillow.
`ConnectedComponents` uses SimpleITK from its isolated `EnvironmentSpec` runtime rather than requiring SimpleITK in the main process.
New domain-specific tools should live in their own packages instead of expanding this package's scope.

## Tests

```bash
uv run pytest packages/bioimageflow-common-tools/tests
```

Package-owned docs live in `docs/`, with one page per public tool under
`docs/tools/`.
