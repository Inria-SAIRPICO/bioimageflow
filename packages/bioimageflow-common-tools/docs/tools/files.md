# Files

`Files` is a source `DataFrameTool` that scans one directory with a glob
pattern and returns one row per matching file. It is the standard lightweight
entry point for local image batches.

Inputs are `path`, the directory to scan, and `pattern`, a glob such as
`*.tif`. The output is a table with a `path` column containing file paths. The
tool sorts matches for deterministic execution and ignores subdirectories.

Use it when files are already local and when recursive discovery or metadata
parsing is not needed. Missing directories currently produce an empty table
rather than a hard failure because `Path.glob` has no matches.

```python
from bioimageflow_common_tools import Files

table = Files().transform(None, type("Args", (), {"path": "images", "pattern": "*.tif"})())
```

## Dependencies and Core Libraries

BioImageFlow DataFrameTool APIs, pathlib, and pandas.

## Assumptions

Files are already local, one directory is enough, and recursive search is not
required.

## Minimal Example

The example above returns a DataFrame with one `path` row per matching TIFF.

## Expected Results

Rows are sorted deterministically by file path.

## Failure Modes

Unreadable directories or permission errors fail through pathlib; unmatched
patterns return an empty DataFrame.
