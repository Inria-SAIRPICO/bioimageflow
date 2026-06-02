# TableFromCsv

`TableFromCsv` loads simple metadata tables from CSV or TSV files. It is a
source `DataFrameTool`, so it does not accept upstream workflow tables.

## Inputs

- `path`: CSV, TSV, or TAB file path.

Files ending in `.csv` are read with comma separators; files ending in `.tsv`
or `.tab` are read with tab separators.

## Outputs

The tool returns a pandas DataFrame with the same columns and rows as the
source file. It does not write a new file.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_common_tools import TableFromCsv

df = TableFromCsv().transform(None, Arguments(path="metadata.csv"))
```

## Dependencies and Core Libraries

pandas and pathlib.

## Assumptions

The table is small enough to hold in memory and uses a standard CSV or TSV
separator inferred from its file suffix.

## Expected Results

The output is the DataFrame returned by `pandas.read_csv` with no index column
configured by the tool.

## Failure Modes

Unsupported suffixes raise `ValueError`. Missing or unreadable files fail
through pandas.
