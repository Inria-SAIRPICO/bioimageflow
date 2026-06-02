# WriteTable

`WriteTable` persists an upstream workflow table to disk while passing the
table through unchanged for downstream nodes.

## Inputs

- upstream table: pandas DataFrame from a previous table tool.
- `path`: output CSV, TSV, or TAB file path.

Files ending in `.csv` are written with comma separators; files ending in
`.tsv` or `.tab` are written with tab separators. Parent directories are
created automatically.

## Outputs

- written table path: the file at `path`.
- downstream table: the same DataFrame, passed through unchanged.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_common_tools import WriteTable

result = WriteTable().transform(df, Arguments(path="results/measurements.tsv"))
```

## Dependencies and Core Libraries

pandas and pathlib.

## Assumptions

The upstream value is a pandas DataFrame and should be written without an index
column.

## Expected Results

The output path contains the table in CSV or TSV form. The returned DataFrame
matches the upstream table.

## Failure Modes

Unsupported suffixes raise `ValueError`. Permission errors and invalid
directories fail through pathlib or pandas.
