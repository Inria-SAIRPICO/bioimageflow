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

- [Files](tools/files.md): create a source table from files in a directory.
- [TableFromCsv](tools/table_from_csv.md): load CSV or TSV metadata as a source
  table.
- [Generate](tools/generate.md): create a source table from literal values.
- [InnerJoin](tools/inner_join.md): merge upstream rows on their index.
- [CrossJoin](tools/cross_join.md): create all row combinations.
- [JoinOnColumn](tools/join_on_column.md): join tables on a named column.
- [Concat](tools/concat.md): append rows from multiple tables.
- [Collect](tools/collect.md): collect ancestor columns into one table.
- [FilterTableRows](tools/filter_table_rows.md): filter rows by a simple
  predicate.
- [SelectColumns](tools/select_columns.md): keep and optionally rename columns.
- [WriteTable](tools/write_table.md): persist an upstream table to CSV or TSV.
- [ExtractChannel](tools/extract_channel.md): write one channel from a
  channel-first image.
- [ConnectedComponents](tools/connected_components.md): label foreground
  components in a binary image.
- [LabelOverlaps](tools/label_overlaps.md): count pixel overlaps between label
  images.
- [Mosaic](tools/mosaic.md): build a grid image from workflow rows.

## Demo Workflow

- [Common glue workflow](workflows/common_glue.md): source discovery,
  parameter generation, and table expansion.

## Tests and Demo Data

Package tests live in `tests/` and can be run with:

```bash
uv run pytest packages/bioimageflow-common-tools/tests
```

Current fixtures are generated in temporary directories. The local
`tests/data/` folder is reserved for tiny tables or images when exact expected
outputs are easier to review as files.
