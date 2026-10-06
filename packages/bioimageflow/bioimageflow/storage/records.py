"""Exact immutable-record reads independent of mutable current pointers."""

# Pyright checks the complete contract on Storage; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from collections.abc import Iterable

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bioimageflow.record_shared_assets import RecordSharedAssets

from .common import Path, pd
from .identity import _native_record_dtype, validate_relative_posix_path
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
        self, result_key: str, record_id: str, *,
        path_columns: Iterable[str] = (), shared_array_columns: Iterable[str] = (),
        hydrate_assets: bool = False,
    ) -> pd.DataFrame:
        """Read a validated exact record dataframe without reselecting current."""
        return self.load_record(
            result_key, record_id, path_columns=path_columns,
            shared_array_columns=shared_array_columns, hydrate_assets=hydrate_assets,
        )[1]

    def load_record(
        self,
        result_key: str,
        record_id: str,
        *,
        path_columns: Iterable[str] = (),
        shared_array_columns: Iterable[str] = (),
        hydrate_assets: bool = False,
    ) -> tuple[RecordManifest, pd.DataFrame, Path]:
        """Admit one exact record, returning its manifest, frame and owned address.

        Transport/logical identity, assets and path containment are validated
        together, with one parquet read and no current-pointer consultation.
        """
        return self._load_record_with_shared_assets(
            result_key, record_id, path_columns=path_columns,
            shared_array_columns=shared_array_columns, hydrate_assets=hydrate_assets,
        )

    def _load_record_with_shared_assets(
        self, result_key: str, record_id: str, *,
        path_columns: Iterable[str] = (), shared_array_columns: Iterable[str] = (),
        hydrate_assets: bool = False, accepted_shared_assets: RecordSharedAssets | None = None,
    ) -> tuple[RecordManifest, pd.DataFrame, Path]:
        """Use emitted own-winner references only after the same exact admission."""
        manifest, dataframe, record_dir = self._admit_record(result_key, record_id)

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
            dataframe = self._decode_portable_cells(dataframe, record_dir, manifest, hydrate=False)
            return manifest, dataframe, record_dir

        dataframe = self._rehydrate_record_assets(
            dataframe,
            record_dir,
            manifest,
            path_columns=declared_path_columns,
            shared_array_columns=declared_shared_array_columns,
            accepted_shared_assets=accepted_shared_assets,
        )

        return manifest, dataframe, record_dir

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
        portable_columns = {name for name, kind in declared_column_kinds.items() if kind == "portable_value"}
        self._decode_portable_cells(dataframe, None, manifest, hydrate=False)
        native_outputs = [output for output in manifest.outputs
                          if output.get("asset_role") == "native_array"
                          and output["array"]["column"] not in portable_columns]
        native_columns = {str(output["array"]["column"]) for output in native_outputs}
        array_columns = shared_array_columns | native_columns
        unknown = (path_columns | array_columns) - set(declared_column_kinds)
        if unknown:
            raise CacheCorruptionError(
                f"Exact record asset columns are not declared: {sorted(unknown)!r}"
            )
        owned_assets = {
            str(output.get("path")): output
            for output in manifest.outputs
            if output.get("kind") == "owned_asset"
        }
        for output in native_outputs:
            column, index = output["array"]["column"], output["array"]["row_index"]
            if column not in dataframe or index not in dataframe.index or dataframe.at[index, column] != output["path"]:
                raise CacheCorruptionError("Native array metadata does not identify its dataframe cell")
        for column in path_columns | array_columns:
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
                if column in array_columns and column not in path_columns:
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
        self, dataframe: pd.DataFrame, record_dir: Path, manifest: RecordManifest, *,
        path_columns: set[str], shared_array_columns: set[str],
        accepted_shared_assets: RecordSharedAssets | None = None,
    ) -> pd.DataFrame:
        has_shared = any(output.get("asset_role") == "shared_array" for output in manifest.outputs)
        if not shared_array_columns and not has_shared:
            return self._rehydrate_record_assets_bound(dataframe, record_dir, manifest,
                path_columns=path_columns, shared_array_columns=shared_array_columns,
                accepted_shared_assets=accepted_shared_assets)
        import uuid
        from bioimageflow_core import get_shared_memory_context
        from bioimageflow.result_groups import map_shared_values as publish_frame
        from bioimageflow.result_groups import bind_result_group

        scope = get_shared_memory_context().task_scope("record_" + uuid.uuid4().hex)
        try:
            with scope.activate():
                hydrated = self._rehydrate_record_assets_bound(dataframe, record_dir, manifest,
                    path_columns=path_columns, shared_array_columns=shared_array_columns,
                    accepted_shared_assets=accepted_shared_assets)
            sealed = publish_frame(hydrated, scope.publish_value)
            sealed, _group = bind_result_group(sealed, node_name="record", group_id=scope.scope_id)
            scope.discard_unreturned()
            return sealed
        except BaseException:
            scope.close()
            raise

    def _rehydrate_record_assets_bound(
        self,
        dataframe: pd.DataFrame,
        record_dir: Path,
        manifest: RecordManifest,
        *,
        path_columns: set[str],
        shared_array_columns: set[str],
        accepted_shared_assets: RecordSharedAssets | None = None,
    ) -> pd.DataFrame:
        hydrated = pd.DataFrame(dataframe, copy=True)
        portable_columns = {str(column["name"]) for column in manifest.dataframe_logical_schema
                            if column["kind"] == "portable_value"}
        native_outputs = {str(output["path"]): output for output in manifest.outputs
                          if output.get("asset_role") == "native_array"
                          and output["array"]["column"] not in portable_columns}
        native_columns = {str(output["array"]["column"]) for output in native_outputs.values()}
        for column in native_columns:
            def rehydrate_native(value: object) -> object:
                if value is None or (isinstance(value, float) and bool(pd.isna(value))):
                    return value
                if not isinstance(value, str) or value not in native_outputs:
                    raise CacheCorruptionError("Native array cell has no exact asset metadata")
                import numpy as np
                from bioimageflow_core import accept_native_array
                path = self._confined_record_path(record_dir, value)
                try:
                    expected_dtype = _native_record_dtype(native_outputs[value]["array"]["dtype"])
                    loaded = np.load(path, allow_pickle=False, mmap_mode="r")
                    return accept_native_array(loaded.view(expected_dtype))
                except (OSError, ValueError, TypeError) as exc:
                    raise CacheCorruptionError(f"Native array asset is unreadable: {value}") from exc
            hydrated[column] = pd.Series([rehydrate_native(value) for value in hydrated[column]],
                                          index=hydrated.index, dtype=object)
        shared_outputs = {
            str(output.get("path")): output
            for output in manifest.outputs
            if output.get("kind") == "owned_asset"
            and output.get("asset_role") == "shared_array"
        }
        for column in shared_array_columns - portable_columns:
            if column not in hydrated.columns:
                continue

            def rehydrate_shared(index: object, value: object) -> object:
                if not isinstance(value, str) or not value.startswith("assets/shm/"):
                    return value
                output = shared_outputs.get(value)
                if output is None:
                    raise CacheCorruptionError(
                        f"Exact shared-array asset is missing metadata: {value}"
                    )
                path = self._confined_record_path(record_dir, value)
                if accepted_shared_assets is not None:
                    reference = accepted_shared_assets.resolve(output, column=column, row_index=str(index))
                    if reference is not None:
                        return reference
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

            hydrated[column] = pd.Series(
                [rehydrate_shared(index, value) for index, value in hydrated[column].items()],
                index=hydrated.index, dtype=object)

        for column in path_columns - portable_columns:
            if column not in hydrated.columns:
                continue

            def rehydrate_path(value: object) -> object:
                if not isinstance(value, str) or not value.startswith("assets/"):
                    return value
                return str(self._confined_record_path(record_dir, value))

            hydrated[column] = hydrated[column].map(rehydrate_path)
        return self._decode_portable_cells(hydrated, record_dir, manifest, hydrate=True,
            accepted_shared_assets=accepted_shared_assets)

    def _decode_portable_cells(
        self, dataframe: pd.DataFrame, record_dir: Path | None, manifest: RecordManifest, *, hydrate: bool,
        accepted_shared_assets: RecordSharedAssets | None = None,
    ) -> pd.DataFrame:
        from bioimageflow.portable_cells import admit_record_cell
        import numpy as np
        from bioimageflow_core import accept_native_array
        from bioimageflow_core.shm import create_shared_output

        columns = {str(column["name"]) for column in manifest.dataframe_logical_schema
                   if column["kind"] == "portable_value"}
        if not columns:
            return dataframe
        frame = pd.DataFrame(dataframe, copy=True)
        referenced: set[str] = set()
        for column in columns:
            values = []
            for index, text in dataframe[column].items():
                def hydrate_asset(output: dict, role: str) -> object:
                    assert record_dir is not None
                    path = self._confined_record_path(record_dir, output["path"])
                    if role == "owned_path":
                        return path
                    if role == "shared_array" and accepted_shared_assets is not None:
                        reference = accepted_shared_assets.resolve(output, column=column, row_index=str(index))
                        if reference is not None:
                            return reference
                    array = np.load(path, allow_pickle=False)
                    if role == "native_array":
                        return accept_native_array(array.view(_native_record_dtype(output["array"]["dtype"])))
                    with create_shared_output(array) as reference:
                        return reference
                try:
                    value, paths = admit_record_cell(text, manifest.outputs, column=column, row_index=str(index),
                        hydrate_asset=hydrate_asset if hydrate else None)
                    values.append(value)
                    referenced.update(paths)
                except (TypeError, ValueError, KeyError, OSError) as exc:
                    raise CacheCorruptionError(f"Invalid portable cell {column!r} at {index!r}") from exc
            frame[column] = pd.Series(values, index=dataframe.index, dtype=object)
        expected = {str(output["path"]) for output in manifest.outputs
                    if output.get("asset_role") in {"native_array", "shared_array"}
                    and output["array"]["column"] in columns}
        if referenced != expected:
            raise CacheCorruptionError("Portable array assets are not exactly referenced by their cells")
        return frame

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
