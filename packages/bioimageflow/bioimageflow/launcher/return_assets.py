"""Typed asset catalogs and locators for submitted public returns."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from bioimageflow.storage import (
    Storage,
    asset_digest_and_size,
    validate_relative_posix_path,
)

from .errors import LauncherProtocolError
from .return_routes import ReturnProviderRoute


def confined_path(path: Path, root: Path, *, label: str) -> Path:
    """Validate that a real, non-symlink path remains under its root."""
    if path.is_symlink():
        raise LauncherProtocolError(f"{label} must not be a symlink.")
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise LauncherProtocolError(f"{label} escapes its assigned root.") from exc
    return path


def catalog_run_assets(
    storage: Storage,
    run_id: str,
    outcomes: Iterable[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Catalog exact immutable and transient provider assets for one run."""
    assets: list[dict[str, Any]] = []
    transients: list[dict[str, Any]] = []
    for outcome in outcomes:
        if outcome.result_key is not None:
            manifest = storage.load_record_manifest(
                outcome.result_key,
                outcome.record_id,
            )
            outputs = {
                str(output["path"]): output
                for output in manifest.outputs
                if output.get("kind") == "owned_asset"
            }
            frame = storage.load_record_dataframe(
                outcome.result_key,
                outcome.record_id,
                path_columns=outcome.path_columns,
                shared_array_columns=outcome.shared_array_columns,
                hydrate_assets=False,
            )
            for column in (
                set(outcome.owned_path_columns)
                | set(outcome.shared_array_columns)
            ):
                if column not in frame.columns:
                    continue
                for row_position, (row_index, stored) in enumerate(
                    frame[column].items()
                ):
                    if not isinstance(stored, str) or not stored.startswith(
                        "assets/"
                    ):
                        continue
                    relative = validate_relative_posix_path(stored)
                    metadata = outputs.get(relative)
                    if metadata is None:
                        raise LauncherProtocolError(
                            "Provider record asset is missing manifest metadata."
                        )
                    assets.append(
                        {
                            "node_key": outcome.node_key,
                            "provider_column": column,
                            "row_position": row_position,
                            "row_index": str(row_index),
                            "result_key": outcome.result_key,
                            "record_id": outcome.record_id,
                            "asset_path": relative,
                            "path": storage.resolve_record_asset(
                                outcome.result_key,
                                outcome.record_id,
                                relative,
                            ),
                            "metadata": metadata,
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
            confined_path(root, storage.cache_root, label="Transient asset root")
            transients.append(
                {
                    "node_key": outcome.node_key,
                    "invocation_id": outcome.transient_invocation_id,
                    "root": root,
                }
            )
    return assets, transients


def base_locator(
    *,
    frame_id: str,
    mapping_key: str | None,
    row_position: int,
    row_index: Any,
    column: str,
) -> dict[str, Any]:
    """Build the common strict locator envelope for one DataFrame cell."""
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
    path: str | Path,
    *,
    catalog: list[dict[str, Any]],
    base: dict[str, Any],
    route: ReturnProviderRoute,
) -> dict[str, Any] | None:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise LauncherProtocolError(
            "Declared provider path output is not absolute."
        )
    resolved = candidate.resolve(strict=False)
    matches = [
        item
        for item in catalog
        if item["node_key"] == route.node_key
        and item["provider_column"] == route.provider_column
        and item["result_key"] == route.result_key
        and item["record_id"] == route.record_id
        and item["metadata"].get("asset_role") != "shared_array"
        and item["path"].resolve(strict=False) == resolved
    ]
    if not matches:
        return None
    same_row = [
        item for item in matches if item["row_index"] == base["row_index"]
    ]
    if same_row:
        matches = same_row
    identities = {
        (item["result_key"], item["record_id"], item["asset_path"])
        for item in matches
    }
    if len(identities) != 1:
        raise LauncherProtocolError(
            "Public return path has ambiguous provider provenance."
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
            "shared_array": None,
        }
    )
    return locator


def _record_shared_locator(
    value: Any,
    *,
    catalog: list[dict[str, Any]],
    base: dict[str, Any],
    route: ReturnProviderRoute,
) -> dict[str, Any] | None:
    from bioimageflow_core.types import SharedArray

    if not isinstance(value, SharedArray):
        return None
    candidates = []
    for item in catalog:
        metadata = item["metadata"]
        array = metadata.get("array")
        if (
            item["node_key"] != route.node_key
            or item["provider_column"] != route.provider_column
            or item["result_key"] != route.result_key
            or item["record_id"] != route.record_id
            or metadata.get("asset_role") != "shared_array"
            or item["row_index"] != base["row_index"]
            or not isinstance(array, dict)
            or list(value.shape) != array.get("shape")
            or value.dtype != array.get("dtype")
        ):
            continue
        candidates.append(item)
    if not candidates:
        return None
    identities = {
        (item["result_key"], item["record_id"], item["asset_path"])
        for item in candidates
    }
    if len(identities) != 1:
        raise LauncherProtocolError(
            "Public shared-array return has ambiguous provider provenance."
        )
    match = candidates[0]
    metadata = match["metadata"]
    array = metadata["array"]
    locator = dict(base)
    locator.update(
        {
            "kind": "record_asset",
            "result_key": match["result_key"],
            "record_id": match["record_id"],
            "asset_path": match["asset_path"],
            "asset_type": "file",
            "digest": metadata["digest"],
            "shared_array": {
                "dtype": array["dtype"],
                "format": array["format"],
                "order": array["order"],
                "shape": array["shape"],
            },
        }
    )
    return locator


def _copy_return_asset(
    source: Path,
    *,
    candidate: Path,
) -> tuple[str, str, str]:
    _size, digest = asset_digest_and_size(source)
    token = digest.removeprefix("sha256:")
    name = source.name or "asset"
    relative = validate_relative_posix_path(
        f"assets/{token[:2]}/{token}/{name}"
    )
    destination = candidate / relative
    if destination.exists():
        _existing_size, existing_digest = asset_digest_and_size(destination)
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
    path: str | Path,
    *,
    transients: list[dict[str, Any]],
    candidate: Path,
    base: dict[str, Any],
    route: ReturnProviderRoute,
) -> dict[str, Any] | None:
    matches: list[Path] = []
    source = Path(path).expanduser()
    if not source.is_absolute():
        raise LauncherProtocolError(
            "Declared transient path output is not absolute."
        )
    resolved = source.resolve(strict=False)
    for transient in transients:
        if (
            transient["node_key"] != route.node_key
            or transient["invocation_id"] != route.transient_invocation_id
        ):
            continue
        root = transient["root"]
        if root.is_symlink() or not root.is_dir():
            continue
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            continue
        if source.is_symlink() or not (source.is_file() or source.is_dir()):
            raise LauncherProtocolError(
                "Transient public return asset is missing or unsafe."
            )
        matches.append(source)
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


def _external_locator(
    path: str | Path,
    *,
    base: dict[str, Any],
) -> dict[str, Any]:
    normalized = Path(path).expanduser()
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


def _one_locator(
    locators: list[dict[str, Any]],
    *,
    label: str,
) -> dict[str, Any] | None:
    by_identity: dict[tuple[Any, ...], dict[str, Any]] = {}
    for locator in locators:
        identity = (
            locator["kind"],
            locator["result_key"],
            locator["record_id"],
            locator["asset_path"],
            locator["path"],
        )
        by_identity.setdefault(identity, locator)
    if len(by_identity) > 1:
        raise LauncherProtocolError(f"{label} has ambiguous provider provenance.")
    return next(iter(by_identity.values()), None)


def locate_path_cell(
    value: str | Path,
    *,
    catalog: list[dict[str, Any]],
    transients: list[dict[str, Any]],
    candidate: Path,
    base: dict[str, Any],
    routes: Sequence[ReturnProviderRoute],
) -> dict[str, Any]:
    """Resolve one declared path through only its compiler-derived providers."""
    records = [
        locator
        for route in routes
        if route.owned and route.result_key is not None
        if (
            locator := _record_path_locator(
                value,
                catalog=catalog,
                base=base,
                route=route,
            )
        )
        is not None
    ]
    selected = _one_locator(records, label="Public return path")
    if selected is not None:
        return selected

    snapshots = [
        locator
        for route in routes
        if route.owned and route.transient_invocation_id is not None
        if (
            locator := _transient_locator(
                value,
                transients=transients,
                candidate=candidate,
                base=base,
                route=route,
            )
        )
        is not None
    ]
    selected = _one_locator(snapshots, label="Public return path")
    if selected is not None:
        return selected

    if any(not route.owned for route in routes):
        return _external_locator(value, base=base)
    raise LauncherProtocolError(
        "Owned return path is not backed by an exact provider outcome."
    )


def locate_shared_array_cell(
    value: Any,
    *,
    catalog: list[dict[str, Any]],
    candidate: Path,
    base: dict[str, Any],
    routes: Sequence[ReturnProviderRoute],
) -> tuple[str, dict[str, Any]]:
    """Resolve a SharedArray to its record or a self-contained snapshot."""
    shared_routes = [route for route in routes if route.shared_array]
    records = [
        locator
        for route in shared_routes
        if route.result_key is not None
        if (
            locator := _record_shared_locator(
                value,
                catalog=catalog,
                base=base,
                route=route,
            )
        )
        is not None
    ]
    selected = _one_locator(records, label="Public shared-array return")
    if selected is not None:
        return selected["asset_path"], selected
    if any(route.result_key is not None for route in shared_routes):
        raise LauncherProtocolError(
            "Record-backed shared-array return has no exact provider asset."
        )
    if shared_routes:
        return _snapshot_shared_array(value, candidate=candidate, base=base)
    raise LauncherProtocolError(
        "Shared-array return cell has no declared provider route."
    )
