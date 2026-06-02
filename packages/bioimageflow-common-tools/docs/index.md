# bioimageflow-common-tools

`bioimageflow-common-tools` is the small glue package for workflows that need
sources, table loading, table joins, filtering, column selection, table export,
simple channel extraction, label overlap checks, connected component labeling,
or visual mosaics. It should stay lightweight and process
orchestration-oriented; domain-heavy segmentation, restoration, spot, tracking,
and large-format IO tools belong in their specialized packages.

Core libraries for the public tools are BioImageFlow, pandas, imageio, NumPy,
Pillow, and SimpleITK for the connected-components wrapper. The package is
intended for users who need to assemble or inspect workflows without installing
deep-learning dependencies.

## Public Tools

- [Files](#files): create a source table from files in a directory.
- [TableFromCsv](#tablefromcsv): load CSV or TSV metadata as a source
  table.
- [Generate](#generate): create a source table from literal values.
- [InnerJoin](#innerjoin): merge upstream rows on their index.
- [CrossJoin](#crossjoin): create all row combinations.
- [JoinOnColumn](#joinoncolumn): join tables on a named column.
- [Concat](#concat): append rows from multiple tables.
- [Collect](#collect): collect ancestor columns into one table.
- [FilterTableRows](#filtertablerows): filter rows by a simple
  predicate.
- [SelectColumns](#selectcolumns): keep and optionally rename columns.
- [WriteTable](#writetable): persist an upstream table to CSV or TSV.
- [ExtractChannel](#extractchannel): write one channel from a
  channel-first image.
- [ConnectedComponents](#connectedcomponents): label foreground
  components in a binary image.
- [LabelOverlaps](#labeloverlaps): count pixel overlaps between label
  images.
- [Mosaic](#mosaic): build a grid image from workflow rows.

## Legacy Module Documentation

These wrappers remain documented for existing workflows, but they are not
re-exported from `bioimageflow_common_tools` and should not be treated as
current public common-tools APIs.

- [Atlas](#atlas): legacy module-level wrapper for the Atlas spot detection
  CLI, currently used by existing FISH examples. Its core dependency is the
  external Atlas CLI from the `bioimageit::atlas` conda package.
- [ConvertImage](#convertimage): legacy module-level bioio conversion wrapper.
  Prefer `bioimageflow-io-tools` for new simple OME normalization workflows.
  Its core dependencies are bioio, bioio writer plugins, Pillow, NumPy, and
  tifffile.

## Demo Workflow

- [Common glue workflow](#common-glue-workflow): source discovery,
  parameter generation, and table expansion.

## Tests and Demo Data

Package tests live in `tests/` and can be run with:

```bash
uv run pytest packages/bioimageflow-common-tools/tests
```

Current fixtures are generated in temporary directories. The local
`tests/data/` folder is reserved for tiny tables or images when exact expected
outputs are easier to review as files.
