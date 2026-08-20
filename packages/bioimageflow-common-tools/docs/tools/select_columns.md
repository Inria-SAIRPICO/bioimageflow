# SelectColumns

`SelectColumns` keeps a comma-separated list of columns and optionally renames them.
It is useful before reports, exports, or joins that need a compact table shape.

## Inputs

- upstream table: pandas DataFrame from a previous table tool.
- `columns`: comma-separated list in output order.
- `rename_mapping`: optional comma-separated `old:new` entries.

## Outputs

The output is a DataFrame containing the selected columns, with optional
renames applied.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_common_tools import SelectColumns

selected = SelectColumns().transform(
    df,
    Arguments(columns="sample,score", rename_mapping="score:value"),
)
```

## Dependencies and Core Libraries

pandas.

## Assumptions

All selected columns exist in the upstream table.
Selections and rename sources must not be repeated, rename mappings must refer to selected columns, and final output names must be unique.

## Expected Results

The output table contains only the selected columns, in the requested order, with requested renames applied.

## Failure Modes

Missing selected columns and mappings for unselected columns raise `KeyError`.
Malformed mappings, duplicate selections, repeated rename sources, and final-name collisions raise `ValueError`.
