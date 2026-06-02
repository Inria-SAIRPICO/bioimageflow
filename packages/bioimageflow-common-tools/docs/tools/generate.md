# Generate

`Generate` creates a one-column table from literal values. It is useful for
parameter sweeps, small condition tables, and workflow smoke tests where a
full metadata file would be unnecessary.

Inputs are `column_name` and `values`. The output schema is resolved from
`column_name`; if the name is missing, schema resolution returns `None` because
the column cannot be known statically.

The tool does not validate value types beyond pandas table construction. Use
it for compact lists, not large experimental metadata.

```python
from bioimageflow_core import Arguments
from bioimageflow_common_tools import Generate

df = Generate().transform(None, Arguments(column_name="sigma", values=[1, 2, 3]))
```

## Dependencies and Core Libraries

BioImageFlow DataFrameTool APIs and pandas.

## Assumptions

The values list is small enough to hold in memory and every value can be stored
in a pandas column.

## Minimal Example

The example above creates three rows in a `sigma` column.

## Expected Results

The output schema resolves to the requested column name when `column_name` is
known.

## Failure Modes

Missing `column_name` prevents static output-schema resolution; invalid values
fail through pandas construction.
