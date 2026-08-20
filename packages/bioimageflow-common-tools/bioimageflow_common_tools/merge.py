"""Built-in merge DataFrameTools."""

from typing import Annotated, Any, TypeGuard

import pandas as pd

from bioimageflow_core.tool import Category, IOModel
from bioimageflow_core.types import Connectable, GUIMeta
from bioimageflow.dataframe_tool import DataFrameTool, Passthrough


def _any_field() -> dict[str, Any]:
    """Per-field schema entry for a column whose type is unknown / variable."""
    return {"type": "any", "default": None, "image_spec": None}


def _all_resolved(
    schemas: list[dict[str, dict[str, Any]] | None],
) -> TypeGuard[list[dict[str, dict[str, Any]]]]:
    """Return ``True`` when every upstream schema is resolved (not ``None``)."""
    return all(s is not None for s in schemas)


def _require_unique_columns(columns: Any, source: str) -> list[Any]:
    names = list(columns)
    seen: set[Any] = set()
    duplicate_set: set[Any] = set()
    duplicates: list[Any] = []
    for name in names:
        if name in seen and name not in duplicate_set:
            duplicates.append(name)
            duplicate_set.add(name)
        seen.add(name)
    if duplicates:
        rendered = ", ".join(repr(name) for name in duplicates)
        raise ValueError(f"{source} contains duplicate column name(s): {rendered}.")
    return names


def _validated_suffixes(value: Any) -> tuple[str, str]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("Column suffixes must contain exactly two strings.")
    left, right = value
    if not isinstance(left, str) or not isinstance(right, str):
        raise ValueError("Column suffixes must contain exactly two strings.")
    if left == right:
        raise ValueError("Column suffixes must be distinct to avoid ambiguous names.")
    return left, right


def _suffixes_for_step(suffixes: tuple[str, str], step: int) -> tuple[str, str]:
    return suffixes if step == 1 else ("", f"_{step + 1}")


def _plan_pair_columns(
    left_columns: Any,
    right_columns: Any,
    *,
    suffixes: tuple[str, str],
    shared_columns: set[Any] | None = None,
) -> tuple[dict[Any, Any], dict[Any, Any], list[Any]]:
    """Plan one merge and reject names that would be duplicated or overwritten."""
    left = _require_unique_columns(left_columns, "Left table")
    right = _require_unique_columns(right_columns, "Right table")
    shared = shared_columns or set()
    overlap = (set(left) & set(right)) - shared
    left_rename = {name: f"{name}{suffixes[0]}" for name in overlap}
    right_rename = {name: f"{name}{suffixes[1]}" for name in overlap}
    output = [left_rename.get(name, name) for name in left]
    output.extend(
        right_rename.get(name, name)
        for name in right
        if name not in shared or name not in left
    )
    duplicates = _require_unique_columns(output, "Planned merge output")
    return left_rename, right_rename, duplicates


def _plan_collected_columns(
    existing_columns: Any,
    incoming_columns: Any,
) -> tuple[dict[Any, Any], list[Any]]:
    existing = _require_unique_columns(existing_columns, "Collected table")
    incoming = _require_unique_columns(incoming_columns, "Incoming table")
    reserved = set(existing) | set(incoming)
    rename: dict[Any, Any] = {}
    for column in incoming:
        if column not in existing:
            continue
        suffix = 1
        candidate = f"{column}_{suffix}"
        while candidate in reserved:
            suffix += 1
            candidate = f"{column}_{suffix}"
        rename[column] = candidate
        reserved.add(candidate)
    output = [*existing, *(rename.get(column, column) for column in incoming)]
    return rename, _require_unique_columns(output, "Planned collection output")


class InnerJoin(DataFrameTool):
    """Inner join upstream DataFrames on index (default merge behavior)."""
    display_name = "Inner Join"
    category = Category.UTILITIES
    tags = ["dataframe", "merge", "join"]

    class Inputs(IOModel):
        pass

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> Any:
        if not dfs:
            return pd.DataFrame()
        result = dfs[0].copy()
        _require_unique_columns(result.columns, "Upstream table 1")
        for position, df in enumerate(dfs[1:], start=2):
            incoming = _require_unique_columns(df.columns, f"Upstream table {position}")
            new_columns = [column for column in incoming if column not in result.columns]
            result = result.join(df.loc[:, new_columns], how="inner")
        return result

    @classmethod
    def resolve_merge_schema(cls, upstream_schemas, inputs=None):
        if not upstream_schemas or not _all_resolved(upstream_schemas):
            return None
        # Mirrors merge_dataframes: left wins on duplicate columns
        # (right duplicates get rsuffix="__bif_dup" then dropped).
        merged: dict[str, dict[str, Any]] = {}
        for schema in upstream_schemas:
            for col, entry in schema.items():
                if col not in merged:
                    merged[col] = entry
        return merged


class CrossJoin(DataFrameTool):
    """Cross join for combinatorial expansion."""
    display_name = "Cross Join"
    category = Category.UTILITIES
    tags = ["dataframe", "merge", "cross-join"]

    class Inputs(IOModel):
        suffixes: Annotated[tuple[str, str], GUIMeta(
            display_name="Column suffixes",
            description="Suffixes added to duplicate column names coming from the left and right DataFrames.",
            connectable=Connectable.NEVER,
        )] = ("_left", "_right")

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> Any:
        suffixes = _validated_suffixes(getattr(arguments, "suffixes", ("_left", "_right")))
        if not dfs:
            return pd.DataFrame()
        if len(dfs) == 1:
            _require_unique_columns(dfs[0].columns, "Upstream table 1")
            return dfs[0].copy()
        result = dfs[0].copy()
        for step, df in enumerate(dfs[1:], 1):
            left_rename, right_rename, _ = _plan_pair_columns(
                result.columns,
                df.columns,
                suffixes=_suffixes_for_step(suffixes, step),
            )
            result = result.rename(columns=left_rename).merge(
                df.rename(columns=right_rename),
                how="cross",
            )
        return result

    @classmethod
    def resolve_merge_schema(cls, upstream_schemas, inputs=None):
        if not upstream_schemas or not _all_resolved(upstream_schemas):
            return None
        suffixes = _validated_suffixes(
            (inputs or {}).get("suffixes", ("_left", "_right"))
        )
        result: dict[str, dict[str, Any]] = dict(upstream_schemas[0])
        for step, schema in enumerate(upstream_schemas[1:], 1):
            left_rename, right_rename, output = _plan_pair_columns(
                result,
                schema,
                suffixes=_suffixes_for_step(suffixes, step),
            )
            result = _planned_schema(
                result,
                schema,
                left_rename,
                right_rename,
                output,
            )
        return result


class JoinOnColumn(DataFrameTool):
    """Join upstream DataFrames on a named column."""
    display_name = "Join On Column"
    category = Category.UTILITIES
    tags = ["dataframe", "merge", "join"]

    class Inputs(IOModel):
        join_column: Annotated[str, GUIMeta(
            display_name="Join column",
            description="Name of the column present in both upstream DataFrames to join on.",
            connectable=Connectable.NEVER,
        )]
        how: Annotated[str, GUIMeta(
            display_name="Join type",
            description="Join strategy: 'inner', 'left', 'right', or 'outer'.",
            connectable=Connectable.NEVER,
        )] = "inner"
        suffixes: Annotated[tuple[str, str], GUIMeta(
            display_name="Column suffixes",
            description="Suffixes added to duplicate column names coming from the left and right DataFrames.",
            connectable=Connectable.NEVER,
        )] = ("_left", "_right")

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> Any:
        join_column = arguments.join_column
        if not isinstance(join_column, str) or not join_column:
            raise ValueError("Join column must be a non-empty string.")
        if arguments.how not in {"inner", "left", "right", "outer"}:
            raise ValueError("Join type must be one of: inner, left, right, outer.")
        suffixes = _validated_suffixes(arguments.suffixes)
        if not dfs:
            return pd.DataFrame()
        if len(dfs) == 1:
            columns = _require_unique_columns(dfs[0].columns, "Upstream table 1")
            if join_column not in columns:
                raise KeyError(f"Join column '{join_column}' is missing from upstream table 1.")
            return dfs[0].copy()
        result = dfs[0].copy()
        for step, df in enumerate(dfs[1:], 1):
            if join_column not in result.columns:
                raise KeyError(f"Join column '{join_column}' is missing from merged table.")
            if join_column not in df.columns:
                raise KeyError(
                    f"Join column '{join_column}' is missing from upstream table {step + 1}."
                )
            left_rename, right_rename, _ = _plan_pair_columns(
                result.columns,
                df.columns,
                suffixes=_suffixes_for_step(suffixes, step),
                shared_columns={join_column},
            )
            result = result.rename(columns=left_rename).merge(
                df.rename(columns=right_rename),
                on=join_column,
                how=arguments.how,
            )
        return result

    @classmethod
    def resolve_merge_schema(cls, upstream_schemas, inputs=None):
        if not upstream_schemas or not _all_resolved(upstream_schemas):
            return None
        join_column = (inputs or {}).get("join_column")
        if not join_column:
            return None
        how = (inputs or {}).get("how", "inner")
        if how not in {"inner", "left", "right", "outer"}:
            raise ValueError("Join type must be one of: inner, left, right, outer.")
        suffixes = _validated_suffixes(
            (inputs or {}).get("suffixes", ("_left", "_right"))
        )
        result: dict[str, dict[str, Any]] = dict(upstream_schemas[0])
        for step, schema in enumerate(upstream_schemas[1:], 1):
            if join_column not in result or join_column not in schema:
                return None
            left_rename, right_rename, output = _plan_pair_columns(
                result,
                schema,
                suffixes=_suffixes_for_step(suffixes, step),
                shared_columns={join_column},
            )
            result = _planned_schema(
                result,
                schema,
                left_rename,
                right_rename,
                output,
                {join_column},
            )
        return result


class Concat(DataFrameTool):
    """Concatenate DataFrames vertically."""
    display_name = "Concat"
    category = Category.UTILITIES
    tags = ["dataframe", "merge", "concat"]

    class Inputs(IOModel):
        pass

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> Any:
        if not dfs:
            return pd.DataFrame()
        for position, df in enumerate(dfs, start=1):
            _require_unique_columns(df.columns, f"Upstream table {position}")
        result = pd.concat(dfs)
        if result.index.duplicated().any():
            # Deduplicate to preserve :: lineage without collisions
            result = result.reset_index(drop=True)
        return result

    @classmethod
    def resolve_merge_schema(cls, upstream_schemas, inputs=None):
        if not upstream_schemas or not _all_resolved(upstream_schemas):
            return None
        # Vertical concat: column union; on type conflict fall back to "any".
        merged: dict[str, dict[str, Any]] = {}
        for schema in upstream_schemas:
            for col, entry in schema.items():
                if col not in merged:
                    merged[col] = entry
                elif merged[col].get("type") != entry.get("type"):
                    merged[col] = _any_field()
        return merged


class Collect(DataFrameTool):
    """Gather columns from multiple ancestor nodes into one DataFrame."""
    display_name = "Collect"
    category = Category.UTILITIES
    tags = ["dataframe", "merge", "collect"]

    class Outputs(Passthrough):
        pass

    class Inputs(IOModel):
        pass

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> Any:
        """Join all DataFrames on index, keeping all columns with numeric suffixes for duplicates."""
        if not dfs:
            return pd.DataFrame()
        if len(dfs) == 1:
            return dfs[0].copy()
        result = dfs[0].copy()
        for df in dfs[1:]:
            rename_map, _ = _plan_collected_columns(result.columns, df.columns)
            result = result.join(df.rename(columns=rename_map), how="inner")
        return result

    @classmethod
    def resolve_merge_schema(cls, upstream_schemas, inputs=None):
        if not upstream_schemas or not _all_resolved(upstream_schemas):
            return None
        # Mirrors merge_dataframes' rename-on-overlap rule: incoming columns
        # that collide get a numeric suffix _1, _2, ... unique against both
        # the accumulator and the incoming schema.
        result: dict[str, dict[str, Any]] = dict(upstream_schemas[0])
        for schema in upstream_schemas[1:]:
            rename_map, _ = _plan_collected_columns(result, schema)
            result = {
                **result,
                **{rename_map.get(column, column): entry for column, entry in schema.items()},
            }
        return result


def _planned_schema(
    left: dict[str, dict[str, Any]],
    right: dict[str, dict[str, Any]],
    left_rename: dict[Any, Any],
    right_rename: dict[Any, Any],
    output: list[Any],
    shared: set[Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Apply a runtime column plan to static schema entries."""
    shared = shared or set()
    entries = [(left_rename.get(name, name), entry) for name, entry in left.items()]
    entries.extend(
        (right_rename.get(name, name), entry)
        for name, entry in right.items()
        if name not in shared or name not in left
    )
    if [name for name, _ in entries] != output:
        raise AssertionError("Merge column plan and schema sources diverged.")
    return dict(entries)
