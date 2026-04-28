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


class InnerJoin(DataFrameTool):
    """Inner join upstream DataFrames on index (default merge behavior)."""
    display_name = "Inner Join"
    category = Category.UTILITIES

    class Inputs(IOModel):
        pass
    # Uses default merge_dataframes (inner join on index)

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

    class Inputs(IOModel):
        suffixes: Annotated[tuple[str, str], GUIMeta(
            display_name="Column suffixes",
            description="Suffixes added to duplicate column names coming from the left and right DataFrames.",
            connectable=Connectable.NEVER,
        )] = ("_left", "_right")

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> Any:
        if not dfs:
            return pd.DataFrame()
        if len(dfs) == 1:
            return dfs[0].copy()
        suffixes: tuple[str, str] = arguments.suffixes if hasattr(arguments, 'suffixes') else ("_left", "_right")
        result = dfs[0]
        for i, df in enumerate(dfs[1:], 1):
            left_suffix = suffixes[0] if i == 1 else ""
            right_suffix = suffixes[1] if i == 1 else f"_{i+1}"
            result = result.merge(df, how="cross", suffixes=(left_suffix, right_suffix))
        return result

    @classmethod
    def resolve_merge_schema(cls, upstream_schemas, inputs=None):
        if not upstream_schemas or not _all_resolved(upstream_schemas):
            return None
        suffixes = (inputs or {}).get("suffixes", ("_left", "_right"))
        # Mirror pandas.merge's suffix-on-overlap semantics. Two-DF: overlap
        # columns get suffixes[0] / suffixes[1]; further DFs at index i (>=2)
        # use "" / f"_{i+1}", matching merge_dataframes above.
        result: dict[str, dict[str, Any]] = dict(upstream_schemas[0])
        for i, schema in enumerate(upstream_schemas[1:], 1):
            left_suffix = suffixes[0] if i == 1 else ""
            right_suffix = suffixes[1] if i == 1 else f"_{i+1}"
            overlap = set(result.keys()) & set(schema.keys())
            new_result: dict[str, dict[str, Any]] = {}
            for col, entry in result.items():
                new_result[f"{col}{left_suffix}" if col in overlap else col] = entry
            for col, entry in schema.items():
                new_result[f"{col}{right_suffix}" if col in overlap else col] = entry
            result = new_result
        return result


class JoinOnColumn(DataFrameTool):
    """Join upstream DataFrames on a named column."""
    display_name = "Join On Column"
    category = Category.UTILITIES

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
        if not dfs:
            return pd.DataFrame()
        if len(dfs) == 1:
            return dfs[0].copy()
        result = dfs[0]
        for df in dfs[1:]:
            result = result.merge(
                df,
                on=arguments.join_column,
                how=arguments.how,
                suffixes=arguments.suffixes,
            )
        return result

    @classmethod
    def resolve_merge_schema(cls, upstream_schemas, inputs=None):
        if not upstream_schemas or not _all_resolved(upstream_schemas):
            return None
        join_column = (inputs or {}).get("join_column")
        if not join_column:
            return None
        suffixes = (inputs or {}).get("suffixes", ("_left", "_right"))
        result: dict[str, dict[str, Any]] = dict(upstream_schemas[0])
        for schema in upstream_schemas[1:]:
            # Overlap minus the join column (which is kept once).
            overlap = (set(result.keys()) & set(schema.keys())) - {join_column}
            new_result: dict[str, dict[str, Any]] = {}
            for col, entry in result.items():
                new_result[f"{col}{suffixes[0]}" if col in overlap else col] = entry
            for col, entry in schema.items():
                if col == join_column and col in result:
                    continue  # already present, kept once
                new_result[f"{col}{suffixes[1]}" if col in overlap else col] = entry
            result = new_result
        return result


class Concat(DataFrameTool):
    """Concatenate DataFrames vertically."""
    display_name = "Concat"
    category = Category.UTILITIES

    class Inputs(IOModel):
        pass

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> Any:
        if not dfs:
            return pd.DataFrame()
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
            # Add numeric suffix (_1, _2, ...) for duplicate columns
            overlap_cols = set(result.columns) & set(df.columns)
            if overlap_cols:
                rename_map = {}
                for col in overlap_cols:
                    suffix = 1
                    new_name = f"{col}_{suffix}"
                    while new_name in result.columns or new_name in df.columns:
                        suffix += 1
                        new_name = f"{col}_{suffix}"
                    rename_map[col] = new_name
                df = df.rename(columns=rename_map)
            result = result.join(df, how="inner")
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
            overlap = set(result.keys()) & set(schema.keys())
            renamed: dict[str, dict[str, Any]] = {}
            for col, entry in schema.items():
                if col in overlap:
                    suffix = 1
                    new_name = f"{col}_{suffix}"
                    while new_name in result or new_name in schema or new_name in renamed:
                        suffix += 1
                        new_name = f"{col}_{suffix}"
                    renamed[new_name] = entry
                else:
                    renamed[col] = entry
            result = {**result, **renamed}
        return result
