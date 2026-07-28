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
from .schemas import RETURN_SCHEMA, validate_return, validate_run_id


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


def _confined(path: Path, root: Path, *, label: str) -> Path:
    if path.is_symlink():
        raise LauncherProtocolError(f"{label} must not be a symlink.")
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise LauncherProtocolError(f"{label} escapes its assigned root.") from exc
    return path


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


def _catalog_for_run(
    storage: Storage,
    run_id: str,
    outcomes: Iterable[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    assets: list[dict[str, Any]] = []
    transients: list[dict[str, Any]] = []
    for outcome in outcomes:
        if outcome.result_key is not None:
            manifest = storage.load_record_manifest(
                outcome.result_key,
                outcome.record_id,
            )
            for output in manifest.outputs:
                if output.get("kind") != "owned_asset":
                    continue
                relative = validate_relative_posix_path(str(output["path"]))
                assets.append(
                    {
                        "node_key": outcome.node_key,
                        "result_key": outcome.result_key,
                        "record_id": outcome.record_id,
                        "asset_path": relative,
                        "path": storage.resolve_record_asset(
                            outcome.result_key,
                            outcome.record_id,
                            relative,
                        ),
                        "metadata": output,
                    }
                )
        if outcome.transient_invocation_id is not None:
            root = (
                storage.transient_invocation_dir(
                    run_id,
                    outcome.node_key,
                    outcome.transient_invocation_id,
                )
                / "assets"
            )
            _confined(root, storage.cache_root, label="Transient asset root")
            transients.append(
                {
                    "node_key": outcome.node_key,
                    "invocation_id": outcome.transient_invocation_id,
                    "root": root,
                }
            )
    return assets, transients


def _base_locator(
    *,
    frame_id: str,
    mapping_key: str | None,
    row_position: int,
    row_index: Any,
    column: str,
) -> dict[str, Any]:
    return {
        "frame_id": frame_id,
        "mapping_key": mapping_key,
        "row_position": row_position,
        "row_index": str(row_index),
        "column": column,
        "kind": None,
        "result_key": None,
        "record_id": None,
        "asset_path": None,
        "path": None,
        "asset_type": None,
        "digest": None,
        "shared_array": None,
    }


def _record_path_locator(
    path: Path,
    *,
    catalog: list[dict[str, Any]],
    base: dict[str, Any],
) -> dict[str, Any] | None:
    resolved = path.resolve(strict=False)
    matches = [
        item for item in catalog if item["path"].resolve(strict=False) == resolved
    ]
    if not matches:
        return None
    if len(matches) != 1:
        identities = {
            (item["result_key"], item["record_id"], item["asset_path"])
            for item in matches
        }
        if len(identities) != 1:
            raise LauncherProtocolError(
                "Public return path matches multiple immutable record assets."
            )
    match = matches[0]
    metadata = match["metadata"]
    locator = dict(base)
    locator.update(
        {
            "kind": "record_asset",
            "result_key": match["result_key"],
            "record_id": match["record_id"],
            "asset_path": match["asset_path"],
            "asset_type": metadata["asset_type"],
            "digest": metadata["digest"],
            "shared_array": metadata.get("array"),
        }
    )
    return locator


def _record_shared_locator(
    value: Any,
    *,
    catalog: list[dict[str, Any]],
    base: dict[str, Any],
) -> dict[str, Any] | None:
    from bioimageflow_core.types import SharedArray

    if not isinstance(value, SharedArray):
        return None
    candidates = [
        item
        for item in catalog
        if item["metadata"].get("asset_role") == "shared_array"
        and item["metadata"].get("array", {}).get("row_index")
        == base["row_index"]
    ]
    exact_column = [
        item
        for item in candidates
        if item["metadata"].get("array", {}).get("column") == base["column"]
    ]
    if exact_column:
        candidates = exact_column
    if not candidates:
        return None
    if len(candidates) != 1:
        raise LauncherProtocolError(
            "Public shared-array return has ambiguous provider provenance."
        )
    match = candidates[0]
    metadata = match["metadata"]
    locator = dict(base)
    locator.update(
        {
            "kind": "record_asset",
            "result_key": match["result_key"],
            "record_id": match["record_id"],
            "asset_path": match["asset_path"],
            "asset_type": "file",
            "digest": metadata["digest"],
            "shared_array": metadata["array"],
        }
    )
    return locator


def _copy_return_asset(
    source: Path,
    *,
    candidate: Path,
) -> tuple[str, str, str]:
    size, digest = asset_digest_and_size(source)
    del size
    token = digest.removeprefix("sha256:")
    name = source.name or "asset"
    relative = validate_relative_posix_path(
        f"assets/{token[:2]}/{token}/{name}"
    )
    destination = candidate / relative
    if destination.exists():
        existing_size, existing_digest = asset_digest_and_size(destination)
        del existing_size
        if existing_digest != digest:
            raise LauncherProtocolError("Return asset digest collision.")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    asset_type = "directory" if source.is_dir() else "file"
    return f"return/{relative}", digest, asset_type


def _transient_locator(
    path: Path,
    *,
    transients: list[dict[str, Any]],
    candidate: Path,
    base: dict[str, Any],
) -> dict[str, Any] | None:
    matches: list[Path] = []
    resolved = path.resolve(strict=False)
    for transient in transients:
        root = transient["root"]
        if root.is_symlink() or not root.is_dir():
            continue
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            continue
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise LauncherProtocolError(
                "Transient public return asset is missing or unsafe."
            )
        matches.append(path)
    if not matches:
        return None
    if len(matches) > 1:
        raise LauncherProtocolError(
            "Public return path matches multiple transient invocations."
        )
    relative, digest, asset_type = _copy_return_asset(
        matches[0],
        candidate=candidate,
    )
    locator = dict(base)
    locator.update(
        {
            "kind": "return_asset",
            "path": relative,
            "asset_type": asset_type,
            "digest": digest,
        }
    )
    return locator


def _snapshot_shared_array(
    value: Any,
    *,
    candidate: Path,
    base: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    import numpy as np
    from bioimageflow_core.shm import open_shared_array
    from bioimageflow_core.types import SharedArray

    if not isinstance(value, SharedArray):
        raise TypeError("value must be a SharedArray.")
    temporary = candidate / f".shared.{uuid.uuid4().hex}.npy"
    with open_shared_array(value) as source:
        array = np.array(source, copy=True, order="C")
    np.save(temporary, array, allow_pickle=False)
    relative, digest, asset_type = _copy_return_asset(
        temporary,
        candidate=candidate,
    )
    temporary.unlink(missing_ok=True)
    locator = dict(base)
    locator.update(
        {
            "kind": "return_asset",
            "path": relative,
            "asset_type": asset_type,
            "digest": digest,
            "shared_array": {
                "dtype": str(array.dtype),
                "format": "npy",
                "order": "C",
                "shape": list(array.shape),
            },
        }
    )
    return relative.removeprefix("return/"), locator


def _external_locator(path: Path, *, base: dict[str, Any]) -> dict[str, Any]:
    normalized = path.expanduser()
    if not normalized.is_absolute():
        normalized = Path.cwd() / normalized
    normalized = normalized.resolve(strict=False)
    locator = dict(base)
    locator.update(
        {
            "kind": "external_reference",
            "path": normalized.as_posix(),
            "asset_type": "external",
        }
    )
    return locator


def persist_public_return(
    control_dir: Path,
    storage_path: str | Path,
    run_id: str,
    value: Any,
    *,
    outcomes: Iterable[Any],
    root_outputs: Sequence[Any] | None = None,
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
        return load_return_manifest(control_dir, expected_run_id=run_id)

    shape, mapping_keys, frames = _normalize_frames(value)
    storage = Storage(storage_path)
    record_assets, transients = _catalog_for_run(storage, run_id, outcomes)
    candidate = control_dir / f".return.{uuid.uuid4().hex}.tmp"
    candidate.mkdir()
    (candidate / "dataframes").mkdir()
    (candidate / "assets").mkdir()
    manifest_frames: list[dict[str, Any]] = []
    locators: list[dict[str, Any]] = []
    try:
        for frame_position, (mapping_key, frame) in enumerate(frames):
            frame_id = f"frame_{frame_position:04d}"
            stored = frame.copy(deep=True)
            for row_position, row_index in enumerate(frame.index):
                for column_position, column_value in enumerate(frame.columns):
                    column = str(column_value)
                    value_at_cell = frame.iat[row_position, column_position]
                    if _missing(value_at_cell):
                        continue
                    base = _base_locator(
                        frame_id=frame_id,
                        mapping_key=mapping_key,
                        row_position=row_position,
                        row_index=row_index,
                        column=column,
                    )
                    from bioimageflow_core.types import SharedArray

                    if isinstance(value_at_cell, SharedArray):
                        locator = _record_shared_locator(
                            value_at_cell,
                            catalog=record_assets,
                            base=base,
                        )
                        if locator is None:
                            stored_value, locator = _snapshot_shared_array(
                                value_at_cell,
                                candidate=candidate,
                                base=base,
                            )
                        else:
                            stored_value = locator["asset_path"]
                        stored.iat[row_position, column_position] = stored_value
                        locators.append(locator)
                        continue
                    if not isinstance(value_at_cell, Path):
                        continue
                    locator = _record_path_locator(
                        value_at_cell,
                        catalog=record_assets,
                        base=base,
                    )
                    if locator is not None:
                        stored.iat[row_position, column_position] = locator[
                            "asset_path"
                        ]
                        locators.append(locator)
                        continue
                    locator = _transient_locator(
                        value_at_cell,
                        transients=transients,
                        candidate=candidate,
                        base=base,
                    )
                    if locator is not None:
                        stored.iat[row_position, column_position] = str(
                            locator["path"]
                        ).removeprefix("return/")
                        locators.append(locator)
                        continue
                    locator = _external_locator(value_at_cell, base=base)
                    stored.iat[row_position, column_position] = locator["path"]
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
        validate_return(manifest)
        manifest_path = candidate / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False)
        )
        _sync_file(manifest_path)
        _sync_dir(candidate / "dataframes")
        _sync_dir(candidate / "assets")
        _sync_dir(candidate)
        _validate_return_tree(candidate, manifest, control_dir=control_dir)
        try:
            os.rename(candidate, installed)
        except FileExistsError:
            shutil.rmtree(candidate)
            existing = load_return_manifest(
                control_dir,
                expected_run_id=run_id,
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
) -> None:
    validate_return(manifest)
    candidate_mode = return_dir.name.startswith(".return.")
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
        _confined(source, return_dir, label="Return DataFrame")
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
        read_dataframe_transport(source, metadata)


def load_return_manifest(
    control_dir: Path,
    *,
    expected_run_id: str | None = None,
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
        manifest = validate_return(json.loads(manifest_path.read_text()))
        if expected_run_id is not None and manifest["run_id"] != expected_run_id:
            raise LauncherProtocolError("Return manifest run ID mismatch.")
        _validate_return_tree(return_dir, manifest, control_dir=control_dir)
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
    manifest = load_return_manifest(control_dir, expected_run_id=run_id)
    storage = Storage(storage_path)
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
                _confined(path, Path(control_dir), label="Return asset")
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
