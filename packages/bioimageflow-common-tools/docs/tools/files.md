# Files

`Files` is a source `DataFrameTool` that creates one row per input file.
It accepts either an explicit ordered `files` list or a `path` directory scan.

`files` and `path` are mutually exclusive and execution raises an error when both or neither are set.
An explicit list preserves the supplied order and every entry must exist and be a regular file.
The directory mode applies `pattern` as a glob and sorts matching regular files for deterministic execution.
Set `recursive` to `true` to apply the pattern below the directory recursively.
`pattern` and `recursive` are ignored when `files` is set.

The output is a table with one `path` column containing file paths.

```python
from pathlib import Path

from bioimageflow_common_tools import Files

arguments = type("Args", (), {
    "path": None,
    "files": [Path("images/a.tif"), Path("images/b.tif")],
    "pattern": "*.png",  # Ignored for explicit files.
    "recursive": False,
})()
table = Files().transform(None, arguments)
```

Directory scanning remains available for local workflows:

```python
arguments = type("Args", (), {
    "path": Path("images"),
    "files": None,
    "pattern": "*.tif",
    "recursive": True,
})()
table = Files().transform(None, arguments)
```

## Dependencies and Core Libraries

BioImageFlow DataFrameTool APIs, pathlib, and pandas.

## Expected Results

Explicit paths keep their input order.
Directory matches are sorted by path.

## Failure Modes

Execution fails when both sources are set, neither source is set, an explicit entry is missing or not a regular file, or the directory source is missing or not a directory.
Unreadable paths and permission failures propagate from pathlib.
An unmatched directory pattern returns an empty DataFrame.
