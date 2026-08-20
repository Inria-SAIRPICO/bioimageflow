"""Main-process table measurement tools."""

from collections.abc import Collection
from typing import Annotated, Any

from bioimageflow import DataFrameTool
from bioimageflow_core import Arguments, Category, Connectable, GUIMeta, IOModel


_DEFAULT_STATS = ("count", "mean", "min", "max", "sum")
_ALLOWED_STATS = {*_DEFAULT_STATS, "median", "std"}
_NUMERIC_SCHEMA_TYPES = {"int", "float"}


def _schema_field(type_name: str) -> dict[str, Any]:
    return {"type": type_name, "default": None, "image_spec": None}


class SummarizeTable(DataFrameTool):
    """Summarize numeric columns in an upstream table."""

    display_name = "Summarize Table"
    documentation = "Summarize numeric columns with count, mean, min, max, and sum."
    category = Category.MEASUREMENT
    tags = ["measurement", "table", "summary"]

    class Inputs(IOModel):
        group_by: Annotated[str | None, GUIMeta(
            display_name="Group by",
            description="Optional grouping column.",
            connectable=Connectable.NEVER,
        )] = None
        columns: Annotated[str | None, GUIMeta(
            display_name="Columns",
            description="Comma-separated numeric columns. Omit to use all numeric columns.",
            connectable=Connectable.NEVER,
        )] = None

    class Outputs(IOModel):
        pass

    @classmethod
    def resolve_merge_schema(cls, upstream_schemas, inputs=None):
        schema = _single_upstream_schema(upstream_schemas)
        if schema is None:
            return None
        settings = inputs or {}
        group_by = settings.get("group_by") or None
        excluded = {group_by} if isinstance(group_by, str) else set()
        columns = _schema_columns(schema, settings.get("columns"), excluded=excluded)
        if columns is None:
            return None
        if group_by:
            if group_by not in schema:
                return None
            result = {group_by: schema[group_by]}
            for column in columns:
                for stat in _DEFAULT_STATS:
                    result[f"{column}_{stat}"] = _schema_field(
                        "int" if stat == "count" else "float"
                    )
            return result
        return {
            "column": _schema_field("str"),
            **{f"value_{stat}": _schema_field("float") for stat in _DEFAULT_STATS},
        }

    def transform(self, df: Any, arguments: Arguments) -> Any:
        import pandas as pd

        table = pd.DataFrame(df)
        grouped_by = getattr(arguments, "group_by", None)
        if grouped_by is not None:
            grouped_by = str(grouped_by).strip()
            if not grouped_by:
                raise ValueError("group_by must be a non-empty column name when provided.")
            if grouped_by not in table.columns:
                raise ValueError(f"Unknown group_by column: {grouped_by}")
        columns = _requested_columns(
            table,
            getattr(arguments, "columns", None),
            excluded={grouped_by} if grouped_by else set(),
        )
        numeric = _numeric_table(table, columns)
        if grouped_by:
            source = table[[grouped_by]].join(numeric)
            result = source.groupby(grouped_by, dropna=False)[columns].agg(
                list(_DEFAULT_STATS)
            )
            result.columns = [f"{column}_{stat}" for column, stat in result.columns]
            _reject_output_collisions([grouped_by, *result.columns])
            return result.reset_index()

        summary = numeric.agg(list(_DEFAULT_STATS)).T
        summary.insert(0, "column", summary.index)
        summary = summary.reset_index(drop=True)
        return summary.rename(columns={stat: f"value_{stat}" for stat in _DEFAULT_STATS})


class AggregatePerImage(DataFrameTool):
    """Aggregate object-level rows into per-image summaries."""

    display_name = "Aggregate Per Image"
    documentation = "Aggregate numeric object-level feature columns by image/sample."
    category = Category.MEASUREMENT
    tags = ["measurement", "table", "aggregate"]

    class Inputs(IOModel):
        group_by: Annotated[str, GUIMeta(
            display_name="Group by",
            description="Image/sample identifier column.",
            connectable=Connectable.NEVER,
        )] = "image"
        columns: Annotated[str | None, GUIMeta(
            display_name="Columns",
            description="Comma-separated numeric columns. Omit to use all numeric columns.",
            connectable=Connectable.NEVER,
        )] = None
        stats: Annotated[str, GUIMeta(
            display_name="Statistics",
            description="Comma-separated aggregations: count, mean, median, min, max, sum, std.",
            connectable=Connectable.NEVER,
        )] = "count,mean,min,max,sum"

    class Outputs(IOModel):
        pass

    @classmethod
    def resolve_merge_schema(cls, upstream_schemas, inputs=None):
        schema = _single_upstream_schema(upstream_schemas)
        if schema is None:
            return None
        settings = inputs or {}
        group_by = settings.get("group_by", "image")
        if not isinstance(group_by, str) or not group_by or group_by not in schema:
            return None
        columns = _schema_columns(schema, settings.get("columns"), excluded={group_by})
        if columns is None:
            return None
        try:
            stats = _requested_stats(settings.get("stats", ",".join(_DEFAULT_STATS)))
        except ValueError:
            return None
        names = [group_by, "object_count", *(f"{column}_{stat}" for column in columns for stat in stats)]
        if len(names) != len(set(names)):
            return None
        result = {group_by: schema[group_by], "object_count": _schema_field("int")}
        for column in columns:
            for stat in stats:
                result[f"{column}_{stat}"] = _schema_field(
                    "int" if stat == "count" else "float"
                )
        return result

    def transform(self, df: Any, arguments: Arguments) -> Any:
        import pandas as pd

        table = pd.DataFrame(df)
        group_by = str(getattr(arguments, "group_by", "image")).strip()
        if not group_by:
            raise ValueError("group_by must be a non-empty column name.")
        if group_by not in table.columns:
            raise ValueError(f"Unknown group_by column: {group_by}")
        columns = _requested_columns(
            table,
            getattr(arguments, "columns", None),
            excluded={group_by},
        )
        stats = _requested_stats(getattr(arguments, "stats", ",".join(_DEFAULT_STATS)))
        output_names = [
            group_by,
            "object_count",
            *(f"{column}_{stat}" for column in columns for stat in stats),
        ]
        _reject_output_collisions(output_names)
        numeric = _numeric_table(table, columns)
        source = table[[group_by]].join(numeric)
        grouped = source.groupby(group_by, dropna=False)
        result = grouped[columns].agg(stats)
        result.columns = [f"{column}_{stat}" for column, stat in result.columns]
        result.insert(0, "object_count", grouped.size().to_numpy())
        return result.reset_index()


class NormalizeFeatures(DataFrameTool):
    """Append normalized versions of numeric feature columns."""

    display_name = "Normalize Features"
    documentation = "Append normalized feature columns using z-score, robust, or min-max."
    category = Category.MEASUREMENT
    tags = ["measurement", "table", "normalize"]

    class Inputs(IOModel):
        columns: Annotated[str | None, GUIMeta(
            display_name="Columns",
            description="Comma-separated numeric columns. Omit to use all numeric columns.",
            connectable=Connectable.NEVER,
        )] = None
        method: Annotated[str, GUIMeta(
            display_name="Method",
            description="Normalization method: zscore, robust, or minmax.",
            connectable=Connectable.NEVER,
        )] = "zscore"
        suffix: Annotated[str, GUIMeta(
            display_name="Suffix",
            description="Non-empty suffix appended to normalized columns.",
            connectable=Connectable.NEVER,
        )] = "_normalized"

    class Outputs(IOModel):
        pass

    @classmethod
    def resolve_merge_schema(cls, upstream_schemas, inputs=None):
        schema = _single_upstream_schema(upstream_schemas)
        if schema is None:
            return None
        settings = inputs or {}
        columns = _schema_columns(schema, settings.get("columns"))
        suffix = settings.get("suffix", "_normalized")
        if columns is None or not isinstance(suffix, str) or not suffix:
            return None
        output_names = [f"{column}{suffix}" for column in columns]
        if len(output_names) != len(set(output_names)) or any(
            name in schema for name in output_names
        ):
            return None
        return {**schema, **{name: _schema_field("float") for name in output_names}}

    def transform(self, df: Any, arguments: Arguments) -> Any:
        import numpy as np
        import pandas as pd

        table = pd.DataFrame(df).copy()
        columns = _requested_columns(table, getattr(arguments, "columns", None))
        method = str(getattr(arguments, "method", "zscore")).strip().lower()
        if method not in {"zscore", "robust", "minmax"}:
            raise ValueError("method must be one of: zscore, robust, minmax")
        suffix = str(getattr(arguments, "suffix", "_normalized"))
        if not suffix:
            raise ValueError("suffix must not be empty.")
        output_columns = [f"{column}{suffix}" for column in columns]
        _reject_output_collisions([*table.columns, *output_columns])

        numeric = _numeric_table(table, columns)
        for column, output_column in zip(columns, output_columns, strict=True):
            values = numeric[column]
            finite = values.dropna()
            if not np.isfinite(finite).all():
                raise ValueError(f"Column {column!r} contains infinite values.")
            if method == "zscore":
                center = values.mean()
                scale = values.std(ddof=0)
            elif method == "robust":
                center = values.median()
                scale = values.quantile(0.75) - values.quantile(0.25)
            else:
                center = values.min()
                scale = values.max() - values.min()
            if pd.isna(scale) or scale == 0:
                table[output_column] = values.where(values.isna(), 0.0)
            else:
                table[output_column] = (values - center) / scale
        return table


def _single_upstream_schema(upstream_schemas):
    if len(upstream_schemas) != 1 or upstream_schemas[0] is None:
        return None
    schema = upstream_schemas[0]
    if "_passthrough" in schema:
        return None
    return schema


def _schema_columns(
    schema: dict[str, dict[str, Any]],
    columns: Any,
    *,
    excluded: Collection[str] = (),
) -> list[str] | None:
    if columns is None:
        selected = [
            name
            for name, entry in schema.items()
            if name not in excluded and entry.get("type") in _NUMERIC_SCHEMA_TYPES
        ]
    elif not isinstance(columns, str) or not columns.strip():
        return None
    else:
        selected = [part.strip() for part in columns.split(",") if part.strip()]
        if len(selected) != len(set(selected)):
            return None
        if any(name not in schema or name in excluded for name in selected):
            return None
    return selected or None


def _requested_columns(
    table: Any,
    columns: str | None,
    *,
    excluded: Collection[str] = (),
) -> list[str]:
    if columns is None:
        requested = [
            column
            for column in table.select_dtypes(include="number").columns
            if column not in excluded
        ]
    else:
        if not isinstance(columns, str) or not columns.strip():
            raise ValueError("columns must contain at least one column name when provided.")
        requested = [column.strip() for column in columns.split(",") if column.strip()]
    if not requested:
        raise ValueError("No numeric columns are available for this operation.")
    duplicates = sorted({name for name in requested if requested.count(name) > 1})
    if duplicates:
        raise ValueError(f"Duplicate selected columns: {duplicates}")
    missing = [column for column in requested if column not in table.columns]
    if missing:
        raise ValueError(f"Unknown numeric columns: {missing}")
    invalid = [column for column in requested if column in excluded]
    if invalid:
        raise ValueError(f"Grouping columns cannot also be summarized: {invalid}")
    return requested


def _numeric_table(table: Any, columns: list[str]) -> Any:
    import pandas as pd

    converted = {}
    for column in columns:
        try:
            converted[column] = pd.to_numeric(table[column], errors="raise")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Column {column!r} must contain numeric values.") from exc
    return pd.DataFrame(converted, index=table.index)


def _requested_stats(stats: str) -> list[str]:
    if not isinstance(stats, str) or not stats.strip():
        raise ValueError("stats must contain at least one aggregation.")
    requested = [stat.strip().lower() for stat in stats.split(",") if stat.strip()]
    duplicates = sorted({stat for stat in requested if requested.count(stat) > 1})
    if duplicates:
        raise ValueError(f"Duplicate aggregation stats: {duplicates}")
    invalid = [stat for stat in requested if stat not in _ALLOWED_STATS]
    if invalid:
        raise ValueError(f"Unsupported aggregation stats: {invalid}")
    return requested


def _reject_output_collisions(names: list[str]) -> None:
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"Output column name collisions: {duplicates}")
