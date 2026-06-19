# bioimageflow-common-tools

`bioimageflow-common-tools` is the small glue package for workflows that need
sources, table loading, table joins, filtering, column selection, table export,
simple channel extraction, label overlap checks, connected component labeling,
or visual mosaics. It should stay lightweight and process
orchestration-oriented; domain-heavy segmentation, restoration, spot, tracking,
and large-format IO tools belong in their specialized packages.

Install-time libraries for the public tools are BioImageFlow, pandas, imageio, NumPy, and Pillow.
`ConnectedComponents` uses SimpleITK and tifffile from its isolated `EnvironmentSpec` runtime rather than requiring them in the main process.
The package is intended for users who need to assemble or inspect workflows without installing deep-learning dependencies.

## Public Tools

- <a href="tools/files.md">Files</a>: create a source table from files in a directory.
- <a href="tools/table_from_csv.md">TableFromCsv</a>: load CSV or TSV metadata as a source
  table.
- <a href="tools/generate.md">Generate</a>: create a source table from literal values.
- <a href="tools/inner_join.md">InnerJoin</a>: merge upstream rows on their index.
- <a href="tools/cross_join.md">CrossJoin</a>: create all row combinations.
- <a href="tools/join_on_column.md">JoinOnColumn</a>: join tables on a named column.
- <a href="tools/concat.md">Concat</a>: append rows from multiple tables.
- <a href="tools/collect.md">Collect</a>: collect ancestor columns into one table.
- <a href="tools/filter_table_rows.md">FilterTableRows</a>: filter rows by a simple
  predicate.
- <a href="tools/select_columns.md">SelectColumns</a>: keep and optionally rename columns.
- <a href="tools/write_table.md">WriteTable</a>: persist an upstream table to CSV or TSV.
- <a href="tools/extract_channel.md">ExtractChannel</a>: write one channel from a
  channel-first image.
- <a href="tools/connected_components.md">ConnectedComponents</a>: label foreground
  components in a binary image.
- <a href="tools/label_overlaps.md">LabelOverlaps</a>: count pixel overlaps between label
  images.
- <a href="tools/mosaic.md">Mosaic</a>: build a grid image from workflow rows.

## Demo Workflow

- <a href="workflows/common_glue.md">Common glue workflow</a>: source discovery,
  parameter generation, and table expansion.

## Tests and Demo Data

Package tests live in `tests/` and can be run with:

```bash
uv run pytest packages/bioimageflow-common-tools/tests
```

Current fixtures are generated in temporary directories. The local
`tests/data/` folder is reserved for tiny tables or images when exact expected
outputs are easier to review as files.
