"""Exact immutable-record reads independent of mutable current pointers."""

# Pyright checks the complete contract on Storage; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from collections.abc import Iterable

from .common import Path, pd
from .identity import validate_relative_posix_path
from .manifests import RecordManifest
from .models import CacheCorruptionError


class _ExactRecordsMixin:
    def load_record_manifest(
        self,
        result_key: str,
        record_id: str,
    ) -> RecordManifest:
        """Validate and return one exact immutable record manifest."""
        return self._load_record_manifest(result_key, record_id)

    def load_record_dataframe(
        self,
        result_key: str,
        record_id: str,
        *,
        path_columns: Iterable[str] = (),
        shared_array_columns: Iterable[str] = (),
        hydrate_assets: bool = False,
    ) -> pd.DataFrame:
        """Load one exact immutable record without consulting ``current.json``."""
        manifest = self._load_record_manifest(result_key, record_id)
        record_dir = self.result_dir(result_key) / "records" / record_id
        dataframe_path = record_dir / "dataframe.parquet"
        try:
            dataframe = pd.read_parquet(dataframe_path)
        except Exception as exc:
            raise CacheCorruptionError("Exact record dataframe is unreadable.") from exc

        declared_path_columns = self._normalize_record_columns(
            path_columns,
            label="path_columns",
        )
        declared_shared_array_columns = self._normalize_record_columns(
            shared_array_columns,
            label="shared_array_columns",
        )
        self._validate_record_asset_references(
            dataframe,
            manifest,
            path_columns=declared_path_columns,
            shared_array_columns=declared_shared_array_columns,
        )
        if not hydrate_assets:
            return dataframe

        return self._rehydrate_record_assets(
            dataframe,
            record_dir,
            manifest,
            path_columns=declared_path_columns,
            shared_array_columns=declared_shared_array_columns,
        )

    def resolve_record_asset(
        self,
        result_key: str,
        record_id: str,
        relative_path: str,
    ) -> Path:
        """Resolve a named immutable record asset after exact manifest validation."""
        manifest = self._load_record_manifest(result_key, record_id)
        try:
            safe_relative = validate_relative_posix_path(relative_path)
        except ValueError as exc:
            raise CacheCorruptionError("Record asset path is unsafe.") from exc
        matching = [
            output
            for output in manifest.outputs
            if output.get("kind") == "owned_asset"
            and output.get("path") == safe_relative
        ]
        if len(matching) != 1:
            raise CacheCorruptionError(
                f"Record asset is not named by the manifest: {safe_relative}"
            )
        record_dir = self.result_dir(result_key) / "records" / record_id
        asset_path = record_dir / safe_relative
        try:
            asset_path.resolve().relative_to(record_dir.resolve())
        except ValueError as exc:
            raise CacheCorruptionError(
                "Record asset escapes its immutable record."
            ) from exc
        return asset_path

    @staticmethod
    def _normalize_record_columns(
        columns: Iterable[str],
        *,
        label: str,
    ) -> set[str]:
        if isinstance(columns, (str, bytes)):
            raise TypeError(f"{label} must be an iterable of column names.")
        normalized = set(columns)
        if any(not isinstance(column, str) or not column for column in normalized):
            raise TypeError(f"{label} must contain only non-empty strings.")
        return normalized

    def _validate_record_asset_references(
        self,
        dataframe: pd.DataFrame,
        manifest: RecordManifest,
        *,
        path_columns: set[str],
        shared_array_columns: set[str],
    ) -> None:
        declared_column_kinds = {
            str(column.get("name")): str(column.get("kind"))
            for column in manifest.dataframe_logical_schema
        }
        unknown = (path_columns | shared_array_columns) - set(declared_column_kinds)
        if unknown:
            raise CacheCorruptionError(
                f"Exact record asset columns are not declared: {sorted(unknown)!r}"
            )
        owned_assets = {
            str(output.get("path")): output
            for output in manifest.outputs
            if output.get("kind") == "owned_asset"
        }
        for column in path_columns | shared_array_columns:
            if column not in dataframe.columns:
                continue
            for value in dataframe[column]:
                if value is None or (isinstance(value, float) and bool(pd.isna(value))):
                    continue
                if not isinstance(value, str):
                    raise CacheCorruptionError(
                        f"Exact record asset column {column!r} contains a non-string value."
                    )
                if value.startswith("assets/"):
                    if declared_column_kinds[column] != "record_asset":
                        raise CacheCorruptionError(
                            f"Exact external-path column {column!r} contains "
                            "a record-relative asset."
                        )
                    try:
                        safe_relative = validate_relative_posix_path(value)
                    except ValueError as exc:
                        raise CacheCorruptionError(
                            "Exact record dataframe contains an unsafe asset path."
                        ) from exc
                    if safe_relative not in owned_assets:
                        raise CacheCorruptionError(
                            f"Exact record asset is missing manifest metadata: {safe_relative}"
                        )
                    continue
                if column in shared_array_columns and column not in path_columns:
                    raise CacheCorruptionError(
                        f"Exact record shared-array column {column!r} contains "
                        "a non-asset value."
                    )
                if declared_column_kinds[column] == "record_asset":
                    raise CacheCorruptionError(
                        f"Exact record-asset column {column!r} contains "
                        "an external path."
                    )
                if column in path_columns and not Path(value).is_absolute():
                    raise CacheCorruptionError(
                        f"Exact record path column {column!r} contains an unsafe relative path."
                    )

    def _rehydrate_record_assets(
        self,
        dataframe: pd.DataFrame,
        record_dir: Path,
        manifest: RecordManifest,
        *,
        path_columns: set[str],
        shared_array_columns: set[str],
    ) -> pd.DataFrame:
        hydrated = dataframe.copy()
        shared_outputs = {
            str(output.get("path")): output
            for output in manifest.outputs
            if output.get("kind") == "owned_asset"
            and output.get("asset_role") == "shared_array"
        }
        for column in shared_array_columns:
            if column not in hydrated.columns:
                continue

            def rehydrate_shared(value: object) -> object:
                if not isinstance(value, str) or not value.startswith("assets/shm/"):
                    return value
                output = shared_outputs.get(value)
                if output is None:
                    raise CacheCorruptionError(
                        f"Exact shared-array asset is missing metadata: {value}"
                    )
                path = self._confined_record_path(record_dir, value)
                try:
                    import numpy as np

                    array = np.load(path, allow_pickle=False)
                except Exception as exc:
                    raise CacheCorruptionError(
                        f"Exact shared-array asset is unreadable: {value}"
                    ) from exc
                from bioimageflow_core.shm import create_shared_output

                with create_shared_output(array) as reference:
                    return reference

            hydrated[column] = hydrated[column].map(rehydrate_shared)

        for column in path_columns:
            if column not in hydrated.columns:
                continue

            def rehydrate_path(value: object) -> object:
                if not isinstance(value, str) or not value.startswith("assets/"):
                    return value
                return str(self._confined_record_path(record_dir, value))

            hydrated[column] = hydrated[column].map(rehydrate_path)
        return hydrated

    @staticmethod
    def _confined_record_path(record_dir: Path, relative_path: str) -> Path:
        try:
            safe_relative = validate_relative_posix_path(relative_path)
        except ValueError as exc:
            raise CacheCorruptionError("Exact record asset path is unsafe.") from exc
        path = record_dir / safe_relative
        try:
            path.resolve().relative_to(record_dir.resolve())
        except ValueError as exc:
            raise CacheCorruptionError(
                "Exact record asset escapes its immutable record."
            ) from exc
        return path
