"""Atomic public-return snapshots for submitted workflow runs."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from bioimageflow.storage import (
    CacheCorruptionError,
    Storage,
    asset_digest_and_size,
    validate_relative_posix_path,
)
from bioimageflow.storage.dataframe_transport import (
    read_dataframe_transport,
    write_dataframe_transport,
)

from .errors import (
    LauncherProtocolError,
    WorkflowRunResultUnavailableError,
)
from .return_assets import (
    base_locator,
    catalog_run_assets,
    confined_path,
    locate_path_cell,
    locate_shared_array_cell,
)
from .return_schema import validate_return_manifest_structure
from .return_routes import (
    DeclaredReturnColumn,
    ReturnProviderRoute,
    ReturnRoutePlan,
)
from .schemas import RETURN_SCHEMA, validate_run_id


def _missing(value: Any) -> bool:
    if isinstance(value, (str, bytes, Path)):
        return False
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    if hasattr(missing, "item"):
        try:
            missing = missing.item()
        except ValueError:
            return False
    return type(missing) is bool and missing


def _sync_file(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    except OSError:
        return


def _sync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _normalize_frames(
    value: Any,
) -> tuple[str, list[str], list[tuple[str | None, pd.DataFrame]]]:
    if isinstance(value, pd.DataFrame):
        return "single", [], [(None, value)]
    if not isinstance(value, Mapping):
        raise TypeError(
            "Submitted workflow return must be a DataFrame or mapping of DataFrames."
        )
    keys: list[str] = []
    frames: list[tuple[str | None, pd.DataFrame]] = []
    for key, frame in value.items():
        if type(key) is not str or not key:
            raise TypeError("Submitted return mapping keys must be non-empty strings.")
        if key in keys:
            raise ValueError("Submitted return mapping keys must be unique.")
        if not isinstance(frame, pd.DataFrame):
            raise TypeError("Submitted return mapping values must be DataFrames.")
        keys.append(key)
        frames.append((key, frame))
    return "mapping", keys, frames


def _normalize_root_outputs(values: Sequence[Any] | None) -> list[dict[str, str]]:
    outputs: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for value in values or ():
        if isinstance(value, Mapping):
            port_id = value.get("port_id")
            name = value.get("name")
        else:
            port_id = getattr(value, "port_id", None)
            name = getattr(value, "name", None)
        if (
            type(port_id) is not str
            or not port_id
            or type(name) is not str
            or not name
            or port_id in seen_ids
            or name in seen_names
        ):
            raise ValueError("Root output IDs and names must be unique strings.")
        seen_ids.add(port_id)
        seen_names.add(name)
        outputs.append({"port_id": port_id, "name": name})
    return outputs


def _route_maps(
    routes: Iterable[ReturnProviderRoute],
) -> tuple[
    dict[tuple[str | None, str], tuple[ReturnProviderRoute, ...]],
    dict[tuple[str | None, str], DeclaredReturnColumn],
]:
    route_values = tuple(routes)
    candidates: dict[
        tuple[str | None, str],
        list[ReturnProviderRoute],
    ] = {}
    declared: dict[tuple[str | None, str], DeclaredReturnColumn] = {}
    for route in route_values:
        if not isinstance(route, ReturnProviderRoute):
            raise TypeError(
                "provider_routes must contain ReturnProviderRoute values."
            )
        key = (route.mapping_key, route.public_column)
        if route not in candidates.setdefault(key, []):
            candidates[key].append(route)
        current = declared.get(key)
        declared[key] = DeclaredReturnColumn(
            mapping_key=route.mapping_key,
            public_column=route.public_column,
            path=(not route.shared_array)
            or (current.path if current is not None else False),
            shared_array=route.shared_array
            or (current.shared_array if current is not None else False),
        )
    if isinstance(routes, ReturnRoutePlan):
        for column in routes.declared_columns:
            if not isinstance(column, DeclaredReturnColumn):
                raise TypeError(
                    "ReturnRoutePlan declarations must be DeclaredReturnColumn values."
                )
            key = (column.mapping_key, column.public_column)
            current = declared.get(key)
            declared[key] = DeclaredReturnColumn(
                mapping_key=column.mapping_key,
                public_column=column.public_column,
                path=column.path
                or (current.path if current is not None else False),
                shared_array=column.shared_array
                or (current.shared_array if current is not None else False),
            )
    return (
        {key: tuple(values) for key, values in candidates.items()},
        declared,
    )


def persist_public_return(
    control_dir: Path,
    storage_path: str | Path,
    run_id: str,
    value: Any,
    *,
    outcomes: Iterable[Any],
    root_outputs: Sequence[Any] | None = None,
    provider_routes: Iterable[ReturnProviderRoute] | ReturnRoutePlan = (),
) -> dict[str, Any]:
    """Stage, validate, and atomically install the exact public return."""
    validate_run_id(run_id)
    control_dir = Path(control_dir)
    if control_dir.is_symlink() or not control_dir.is_dir():
        raise LauncherProtocolError(
            "Launcher control directory must be a real directory."
        )
    installed = control_dir / "return"
    if installed.exists():
        return load_return_manifest(
            control_dir,
            expected_run_id=run_id,
            storage_path=storage_path,
        )

    shape, mapping_keys, frames = _normalize_frames(value)
    storage = Storage(storage_path)
    record_assets, transients = catalog_run_assets(storage, run_id, outcomes)
    routes, declarations = _route_maps(provider_routes)
    candidate = control_dir / f".return.{uuid.uuid4().hex}.tmp"
    candidate.mkdir()
    (candidate / "dataframes").mkdir()
    (candidate / "assets").mkdir()
    manifest_frames: list[dict[str, Any]] = []
    locators: list[dict[str, Any]] = []
    try:
        from bioimageflow_core.types import SharedArray

        for frame_position, (mapping_key, frame) in enumerate(frames):
            frame_id = f"frame_{frame_position:04d}"
            stored = frame.copy(deep=True)
            for row_position, row_index in enumerate(frame.index):
                for column_position, column_value in enumerate(frame.columns):
                    column = str(column_value)
                    value_at_cell = frame.iat[row_position, column_position]
                    if _missing(value_at_cell):
                        continue
                    base = base_locator(
                        frame_id=frame_id,
                        mapping_key=mapping_key,
                        row_position=row_position,
                        row_index=row_index,
                        column=column,
                    )
                    key = (mapping_key, column)
                    route_candidates = routes.get(key, ())
                    declaration = declarations.get(key)
                    if isinstance(value_at_cell, SharedArray):
                        if (
                            declaration is None
                            or not declaration.shared_array
                            or not route_candidates
                        ):
                            raise LauncherProtocolError(
                                "Shared-array return cell has no declared provider route."
                            )
                        stored_value, locator = locate_shared_array_cell(
                            value_at_cell,
                            catalog=record_assets,
                            candidate=candidate,
                            base=base,
                            routes=route_candidates,
                        )
                        stored.iat[row_position, column_position] = stored_value
                        locators.append(locator)
                        continue
                    if declaration is None:
                        if isinstance(value_at_cell, Path):
                            raise LauncherProtocolError(
                                "Path return cell has no declared provider route."
                            )
                        continue
                    if (
                        not declaration.path
                        or not route_candidates
                        or not isinstance(value_at_cell, (str, Path))
                    ):
                        raise LauncherProtocolError(
                            "Declared path return cell has an invalid value."
                        )
                    locator = locate_path_cell(
                        value_at_cell,
                        catalog=record_assets,
                        transients=transients,
                        candidate=candidate,
                        base=base,
                        routes=route_candidates,
                    )
                    stored.iat[row_position, column_position] = (
                        locator["asset_path"]
                        if locator["kind"] == "record_asset"
                        else str(locator["path"]).removeprefix("return/")
                    )
                    locators.append(locator)

            destination = candidate / "dataframes" / f"{frame_id}.parquet"
            metadata = write_dataframe_transport(stored, destination)
            manifest_frames.append(
                {
                    "id": frame_id,
                    "mapping_key": mapping_key,
                    "path": f"return/dataframes/{frame_id}.parquet",
                    **metadata,
                }
            )
            _sync_file(destination)

        manifest = {
            "schema": RETURN_SCHEMA,
            "run_id": run_id,
            "shape": shape,
            "mapping_keys": mapping_keys,
            "frames": manifest_frames,
            "root_outputs": _normalize_root_outputs(root_outputs),
            "locators": locators,
        }
        validate_return_manifest_structure(manifest)
        manifest_path = candidate / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False)
        )
        _sync_file(manifest_path)
        _sync_dir(candidate / "dataframes")
        _sync_dir(candidate / "assets")
        _sync_dir(candidate)
        _validate_return_tree(
            candidate,
            manifest,
            control_dir=control_dir,
            storage=storage,
        )
        try:
            os.rename(candidate, installed)
        except FileExistsError:
            shutil.rmtree(candidate)
            existing = load_return_manifest(
                control_dir,
                expected_run_id=run_id,
                storage_path=storage_path,
            )
            if existing != manifest:
                raise LauncherProtocolError(
                    "A different public return is already installed."
                )
            return existing
        _sync_dir(control_dir)
        return manifest
    except BaseException:
        if candidate.exists():
            shutil.rmtree(candidate)
        raise


def _validate_return_tree(
    return_dir: Path,
    manifest: Mapping[str, Any],
    *,
    control_dir: Path,
    storage: Storage | None,
) -> None:
    manifest = validate_return_manifest_structure(manifest)
    candidate_mode = return_dir.name.startswith(".return.")
    loaded_frames: dict[str, pd.DataFrame] = {}
    for frame in manifest["frames"]:
        if not isinstance(frame, Mapping):
            raise LauncherProtocolError("Return frame metadata must be an object.")
        relative = validate_relative_posix_path(str(frame["path"]))
        if not relative.startswith("return/dataframes/"):
            raise LauncherProtocolError(
                "Return DataFrame path must be under return/dataframes/."
            )
        source = (
            return_dir / Path(relative).relative_to("return")
            if candidate_mode
            else control_dir / relative
        )
        confined_path(source, return_dir, label="Return DataFrame")
        metadata = {
            key: frame[key]
            for key in (
                "index",
                "logical_digest",
                "logical_schema",
                "path_cells",
                "transport_digest",
            )
        }
        loaded_frames[frame["id"]] = read_dataframe_transport(source, metadata)

    for locator in manifest["locators"]:
        frame = loaded_frames[locator["frame_id"]]
        row_position = locator["row_position"]
        column = locator["column"]
        if row_position >= len(frame) or column not in frame.columns:
            raise LauncherProtocolError(
                "Return locator addresses an unknown DataFrame cell."
            )
        if str(frame.index[row_position]) != locator["row_index"]:
            raise LauncherProtocolError(
                "Return locator row identity does not match its DataFrame."
            )
        column_position = list(frame.columns).index(column)
        stored_value = frame.iat[row_position, column_position]
        kind = locator["kind"]
        if kind == "record_asset":
            expected_value = locator["asset_path"]
            if storage is not None:
                try:
                    path = storage.resolve_record_asset(
                        locator["result_key"],
                        locator["record_id"],
                        locator["asset_path"],
                    )
                    _size, digest = asset_digest_and_size(path)
                except (CacheCorruptionError, OSError, ValueError) as exc:
                    raise WorkflowRunResultUnavailableError(
                        "Immutable record required by the submitted result is unavailable.",
                        details={
                            "result_key": locator["result_key"],
                            "record_id": locator["record_id"],
                        },
                    ) from exc
                if digest != locator["digest"]:
                    raise LauncherProtocolError(
                        "Return record asset digest does not match."
                    )
                asset_type = "directory" if path.is_dir() else "file"
                if asset_type != locator["asset_type"]:
                    raise LauncherProtocolError(
                        "Return record asset type does not match."
                    )
        elif kind == "return_asset":
            relative = validate_relative_posix_path(locator["path"])
            path = (
                return_dir / Path(relative).relative_to("return")
                if candidate_mode
                else control_dir / relative
            )
            confined_path(path, return_dir, label="Return asset")
            _size, digest = asset_digest_and_size(path)
            if digest != locator["digest"]:
                raise LauncherProtocolError(
                    "Self-contained return asset digest does not match."
                )
            asset_type = "directory" if path.is_dir() else "file"
            if asset_type != locator["asset_type"]:
                raise LauncherProtocolError(
                    "Self-contained return asset type does not match."
                )
            expected_value = relative.removeprefix("return/")
        else:
            expected_value = locator["path"]
        if str(stored_value) != expected_value:
            raise LauncherProtocolError(
                "Return locator does not match its stored DataFrame cell."
            )


def load_return_manifest(
    control_dir: Path,
    *,
    expected_run_id: str | None = None,
    storage_path: str | Path | None = None,
) -> dict[str, Any]:
    """Load and verify an installed return manifest."""
    control_dir = Path(control_dir)
    return_dir = control_dir / "return"
    manifest_path = return_dir / "manifest.json"
    if (
        return_dir.is_symlink()
        or not return_dir.is_dir()
        or manifest_path.is_symlink()
        or not manifest_path.is_file()
    ):
        raise WorkflowRunResultUnavailableError(
            "Submitted return manifest is unavailable."
        )
    try:
        manifest = validate_return_manifest_structure(
            json.loads(manifest_path.read_text())
        )
        if expected_run_id is not None and manifest["run_id"] != expected_run_id:
            raise LauncherProtocolError("Return manifest run ID mismatch.")
        _validate_return_tree(
            return_dir,
            manifest,
            control_dir=control_dir,
            storage=None if storage_path is None else Storage(storage_path),
        )
    except WorkflowRunResultUnavailableError:
        raise
    except Exception as exc:
        raise WorkflowRunResultUnavailableError(
            "Submitted return manifest is invalid.",
            details={"control_dir": str(control_dir)},
        ) from exc
    return manifest


def _load_shared_array(path: Path, metadata: Mapping[str, Any]) -> Any:
    import numpy as np
    from bioimageflow_core.shm import create_shared_output

    if metadata.get("format") != "npy":
        raise WorkflowRunResultUnavailableError(
            "Shared-array return metadata has an invalid format."
        )
    try:
        array = np.load(path, allow_pickle=False)
    except Exception as exc:
        raise WorkflowRunResultUnavailableError(
            "Shared-array return asset is unavailable."
        ) from exc
    if list(array.shape) != metadata.get("shape") or str(array.dtype) != metadata.get(
        "dtype"
    ):
        raise WorkflowRunResultUnavailableError(
            "Shared-array return asset metadata does not match."
        )
    context = create_shared_output(array)
    reference = context.__enter__()
    context.__exit__(None, None, None)
    return reference


def load_public_return(
    control_dir: Path,
    storage_path: str | Path,
    run_id: str,
) -> Any:
    """Rehydrate a successful public return without consulting current pointers."""
    storage = Storage(storage_path)
    manifest = load_return_manifest(
        control_dir,
        expected_run_id=run_id,
        storage_path=storage_path,
    )
    frames: dict[str, pd.DataFrame] = {}
    for frame in manifest["frames"]:
        source = Path(control_dir) / validate_relative_posix_path(frame["path"])
        metadata = {
            key: frame[key]
            for key in (
                "index",
                "logical_digest",
                "logical_schema",
                "path_cells",
                "transport_digest",
            )
        }
        frames[frame["id"]] = read_dataframe_transport(source, metadata)

    for locator in manifest["locators"]:
        frame = frames[locator["frame_id"]]
        row_position = locator["row_position"]
        column = locator["column"]
        if row_position >= len(frame) or column not in frame.columns:
            raise WorkflowRunResultUnavailableError(
                "Return locator addresses an unknown DataFrame cell."
            )
        kind = locator["kind"]
        if kind == "record_asset":
            try:
                path = storage.resolve_record_asset(
                    locator["result_key"],
                    locator["record_id"],
                    locator["asset_path"],
                )
            except (CacheCorruptionError, OSError, ValueError) as exc:
                raise WorkflowRunResultUnavailableError(
                    "Immutable record required by the submitted result is unavailable.",
                    details={
                        "result_key": locator["result_key"],
                        "record_id": locator["record_id"],
                    },
                ) from exc
            shared = locator.get("shared_array")
            value = _load_shared_array(path, shared) if shared is not None else path
        elif kind == "return_asset":
            relative = validate_relative_posix_path(locator["path"])
            path = Path(control_dir) / relative
            try:
                confined_path(path, Path(control_dir), label="Return asset")
                _size, digest = asset_digest_and_size(path)
            except Exception as exc:
                raise WorkflowRunResultUnavailableError(
                    "Self-contained return asset is unavailable."
                ) from exc
            if digest != locator["digest"]:
                raise WorkflowRunResultUnavailableError(
                    "Self-contained return asset digest mismatch."
                )
            shared = locator.get("shared_array")
            value = _load_shared_array(path, shared) if shared is not None else path
        elif kind == "external_reference":
            value = Path(locator["path"])
            if not value.is_absolute():
                raise WorkflowRunResultUnavailableError(
                    "External return reference is not absolute."
                )
        else:
            raise WorkflowRunResultUnavailableError(
                f"Unknown return locator kind {kind!r}."
            )
        column_position = list(frame.columns).index(column)
        frame.iat[row_position, column_position] = value

    ordered_frames = [frames[frame["id"]] for frame in manifest["frames"]]
    if manifest["shape"] == "single":
        if len(ordered_frames) != 1:
            raise WorkflowRunResultUnavailableError(
                "Single return manifest has the wrong frame count."
            )
        return ordered_frames[0]
    if manifest["shape"] != "mapping" or len(ordered_frames) != len(
        manifest["mapping_keys"]
    ):
        raise WorkflowRunResultUnavailableError(
            "Mapping return manifest has the wrong frame count."
        )
    return {
        key: frame
        for key, frame in zip(
            manifest["mapping_keys"],
            ordered_frames,
            strict=True,
        )
    }
