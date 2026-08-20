"""Lightweight table source, filtering, selection, and persistence tools."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, cast

import pandas as pd

from bioimageflow import DataFrameTool, Passthrough
from bioimageflow_core import Category, Connectable, GUIMeta, IOModel


_CSV_SUFFIXES = {".csv"}
_TSV_SUFFIXES = {".tsv", ".tab"}
_FILTER_OPERATORS = {"eq", "ne", "gt", "ge", "lt", "le", "contains", "in"}


def _separator_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _CSV_SUFFIXES:
        return ","
    if suffix in _TSV_SUFFIXES:
        return "\t"
    raise ValueError(f"Expected a CSV or TSV path, got '{path}'.")


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_rename_mapping(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    mapping: dict[str, str] = {}
    for item in _split_csv(value):
        if ":" not in item:
            raise ValueError(
                "Rename mapping entries must use 'old:new' comma-separated syntax."
            )
        old, new = (part.strip() for part in item.split(":", 1))
        if not old or not new:
            raise ValueError("Rename mapping entries must include old and new names.")
        if old in mapping:
            raise ValueError(f"Rename mapping contains duplicate source column '{old}'.")
        mapping[old] = new
    return mapping


def _validate_column_selection(
    available_columns: Any,
    columns: list[str],
    rename_mapping: dict[str, str],
) -> list[str]:
    available = list(available_columns)
    duplicated_available = list(dict.fromkeys(
        column
        for index, column in enumerate(available)
        if column in available[:index]
    ))
    if duplicated_available:
        raise ValueError(
            "Upstream table contains duplicate column name(s): "
            f"{', '.join(str(column) for column in duplicated_available)}."
        )
    duplicates = list(dict.fromkeys(
        column for index, column in enumerate(columns) if column in columns[:index]
    ))
    if duplicates:
        raise ValueError(
            f"Columns contains duplicate selection(s): {', '.join(duplicates)}."
        )
    unused = sorted(set(rename_mapping) - set(columns))
    if unused:
        raise KeyError(
            f"Rename mapping references unselected column(s): {', '.join(unused)}"
        )
    final_names = [rename_mapping.get(column, column) for column in columns]
    collisions = list(dict.fromkeys(
        name for index, name in enumerate(final_names) if name in final_names[:index]
    ))
    if collisions:
        raise ValueError(
            "Selected and renamed columns must have unique output names; "
            f"collision(s): {', '.join(collisions)}."
        )
    return final_names


def _coerce_value_for_series(series: pd.Series, value: str) -> Any:
    if pd.api.types.is_bool_dtype(series.dtype):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
        raise ValueError(f"Cannot compare boolean column '{series.name}' to '{value}'.")
    if pd.api.types.is_numeric_dtype(series.dtype):
        return pd.to_numeric(value)
    return value


def _numeric_series_and_value(series: pd.Series, value: str) -> tuple[pd.Series, Any]:
    try:
        return cast(pd.Series, pd.to_numeric(series)), pd.to_numeric(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Operator requires numeric values for column '{series.name}'."
        ) from exc


def _passthrough_schema(
    upstream_schemas: list[dict[str, dict[str, Any]] | None],
) -> dict[str, dict[str, Any]] | None:
    if not upstream_schemas or upstream_schemas[0] is None:
        return None
    return dict(upstream_schemas[0])


class TableFromCsv(DataFrameTool):
    """Load a CSV or TSV metadata table as a workflow source."""

    display_name = "Table From CSV"
    documentation = "Load a CSV or TSV file into a pandas DataFrame."
    category = Category.UTILITIES
    tags = ["source", "dataframe", "metadata"]
    accepts_upstream = False

    class Inputs(IOModel):
        path: Annotated[
            Path,
            GUIMeta(
                display_name="Table path",
                description="Path to a .csv, .tsv, or .tab metadata table.",
                connectable=Connectable.NEVER,
            ),
        ]

    def transform(self, df: Any, arguments: Any) -> pd.DataFrame:
        path = Path(arguments.path)
        return pd.read_csv(path, sep=_separator_for_path(path))


class WriteTable(DataFrameTool):
    """Persist an upstream workflow table to CSV or TSV."""

    display_name = "Write Table"
    documentation = "Write the upstream DataFrame to a CSV or TSV path."
    category = Category.UTILITIES
    tags = ["dataframe", "writer"]

    class Inputs(IOModel):
        path: Annotated[
            Path,
            GUIMeta(
                display_name="Output path",
                description="Destination .csv, .tsv, or .tab table path.",
                connectable=Connectable.NEVER,
            ),
        ]

    class Outputs(Passthrough):
        pass

    def transform(self, df: pd.DataFrame, arguments: Any) -> pd.DataFrame:
        path = Path(arguments.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, sep=_separator_for_path(path), index=False)
        return df.copy()


class FilterTableRows(DataFrameTool):
    """Filter rows by one column, one operator, and one literal value."""

    display_name = "Filter Table Rows"
    documentation = (
        "Filter an upstream DataFrame with a deterministic column/operator/value "
        "predicate."
    )
    category = Category.UTILITIES
    tags = ["dataframe", "filter"]

    class Inputs(IOModel):
        column: Annotated[
            str,
            GUIMeta(
                display_name="Column",
                description="Column used for filtering.",
                connectable=Connectable.NEVER,
            ),
        ]
        operator: Annotated[
            str,
            GUIMeta(
                display_name="Operator",
                description="One of eq, ne, gt, ge, lt, le, contains, or in.",
                connectable=Connectable.NEVER,
            ),
        ] = "eq"
        value: Annotated[
            str,
            GUIMeta(
                display_name="Value",
                description="Literal comparison value. For 'in', use comma-separated values.",
                connectable=Connectable.NEVER,
            ),
        ] = ""

    class Outputs(Passthrough):
        pass

    def transform(self, df: pd.DataFrame, arguments: Any) -> pd.DataFrame:
        column = arguments.column
        operator = arguments.operator
        value = str(arguments.value)
        if column not in df.columns:
            raise KeyError(f"Column '{column}' does not exist.")
        if operator not in _FILTER_OPERATORS:
            raise ValueError(f"Unsupported operator '{operator}'.")

        series = cast(pd.Series, df[column])
        if operator == "contains":
            mask = series.astype(str).str.contains(value, regex=False, na=False)
        elif operator == "in":
            values = [_coerce_value_for_series(series, item) for item in _split_csv(value)]
            mask = series.isin(values)
        elif operator in {"gt", "ge", "lt", "le"}:
            numeric_series, numeric_value = _numeric_series_and_value(series, value)
            if operator == "gt":
                mask = numeric_series > numeric_value
            elif operator == "ge":
                mask = numeric_series >= numeric_value
            elif operator == "lt":
                mask = numeric_series < numeric_value
            else:
                mask = numeric_series <= numeric_value
        else:
            compare_value = _coerce_value_for_series(series, value)
            if operator == "eq":
                mask = series == compare_value
            else:
                mask = series != compare_value

        return df.loc[mask].copy()

    @classmethod
    def resolve_merge_schema(cls, upstream_schemas, inputs=None):
        return _passthrough_schema(upstream_schemas)


class SelectColumns(DataFrameTool):
    """Keep selected columns and optionally rename them."""

    display_name = "Select Columns"
    documentation = "Keep comma-separated columns and optionally rename old:new pairs."
    category = Category.UTILITIES
    tags = ["dataframe", "columns"]

    class Inputs(IOModel):
        columns: Annotated[
            str,
            GUIMeta(
                display_name="Columns",
                description="Comma-separated column names to keep, in output order.",
                connectable=Connectable.NEVER,
            ),
        ]
        rename_mapping: Annotated[
            str,
            GUIMeta(
                display_name="Rename mapping",
                description="Optional comma-separated old:new rename mapping.",
                connectable=Connectable.NEVER,
            ),
        ] = ""

    def transform(self, df: pd.DataFrame, arguments: Any) -> pd.DataFrame:
        columns = _split_csv(arguments.columns)
        if not columns:
            raise ValueError("At least one column must be selected.")
        missing = [column for column in columns if column not in df.columns]
        if missing:
            raise KeyError(f"Missing column(s): {', '.join(missing)}")

        rename_mapping = _parse_rename_mapping(arguments.rename_mapping)
        _validate_column_selection(df.columns, columns, rename_mapping)
        return df.loc[:, columns].rename(columns=rename_mapping).copy()

    @classmethod
    def resolve_merge_schema(cls, upstream_schemas, inputs=None):
        upstream = _passthrough_schema(upstream_schemas)
        columns = _split_csv((inputs or {}).get("columns", ""))
        if upstream is None or not columns:
            return None
        missing = [column for column in columns if column not in upstream]
        if missing:
            return None
        rename_mapping = _parse_rename_mapping((inputs or {}).get("rename_mapping", ""))
        final_names = _validate_column_selection(upstream, columns, rename_mapping)
        return {
            final_name: upstream[column]
            for column, final_name in zip(columns, final_names)
        }
