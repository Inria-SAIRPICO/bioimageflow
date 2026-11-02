# bioimageflow-common-tools

`bioimageflow-common-tools` provides lightweight workflow glue for BioImageFlow.
It focuses on source tables, table reshaping, simple joins, small image helpers, report-friendly exports, and basic OMERO image transfers.
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
- `OmeroDownloadDataset`, `OmeroUploadImage`: lossless basic OMERO transfers with worker-only dependencies.

## Dependencies

Install-time libraries are BioImageFlow and pandas.
Image-processing tools import imageio, scikit-image, NumPy, and Pillow only inside worker methods and obtain them from the shared `GENERAL_ENV`.
The OMERO declarations use only Core and the standard library; OMERO.py, Ice, keyring and their image readers remain in the tools' pinned managed worker environment.
OMERO discovery exports are available on macOS arm64/x86_64 and Linux x86_64; the Linux worker requires glibc >=2.28.
Other hosts retain all ordinary common tools, while explicit OMERO imports or recipe construction report the unsupported platform.
See the [OMERO tool documentation](docs/tools/omero_download_dataset.md) for pixel limits, secret-free connection fields, caching and rollback boundaries.
New domain-specific tools should live in their own packages instead of expanding this package's scope.

## Migration

`ExtractChannel` has moved out of this package.
Use `SelectChannel` from `bioimageflow-io-tools` for channel selection.

## Tests

```bash
uv run pytest packages/bioimageflow-common-tools/tests
```

Package-owned docs live in `docs/`, with one page per public tool under `docs/tools/`.
