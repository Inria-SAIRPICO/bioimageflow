"""Strict nested validation for launcher public-return manifests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import LauncherProtocolError
from .schemas import validate_return


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_FRAME_FIELDS = frozenset(
    {
        "id",
        "mapping_key",
        "path",
        "logical_schema",
        "logical_digest",
        "transport_digest",
        "index",
        "path_cells",
    }
)
_ROOT_OUTPUT_FIELDS = frozenset({"port_id", "name"})
_LOCATOR_FIELDS = frozenset(
    {
        "frame_id",
        "mapping_key",
        "row_position",
        "row_index",
        "column",
        "kind",
        "result_key",
        "record_id",
        "asset_path",
        "path",
        "asset_type",
        "digest",
        "shared_array",
    }
)
_SHARED_ARRAY_FIELDS = frozenset({"dtype", "format", "order", "shape"})


def _exact_mapping(
    value: Any,
    fields: frozenset[str],
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise LauncherProtocolError(f"{label} must be an object.")
    result = dict(value)
    if frozenset(result) != fields:
        raise LauncherProtocolError(f"{label} has missing or unknown fields.")
    return result


def _nonempty_string(value: Any, *, label: str) -> str:
    if type(value) is not str or not value:
        raise LauncherProtocolError(f"{label} must be a non-empty string.")
    return value


def _optional_string(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, label=label)


def _digest(value: Any, *, label: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise LauncherProtocolError(
            f"{label} must be a lowercase SHA-256 digest."
        )
    return value


def _relative_path(value: Any, *, prefix: str, label: str) -> str:
    path = _nonempty_string(value, label=label)
    if (
        path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or not path.startswith(prefix)
    ):
        raise LauncherProtocolError(
            f"{label} must be confined beneath {prefix!r}."
        )
    return path


def _shared_array(value: Any, *, label: str) -> None:
    if value is None:
        return
    shared = _exact_mapping(value, _SHARED_ARRAY_FIELDS, label=label)
    _nonempty_string(shared["dtype"], label=f"{label}.dtype")
    if shared["format"] != "npy" or shared["order"] != "C":
        raise LauncherProtocolError(
            f"{label} must use C-order NumPy transport."
        )
    shape = shared["shape"]
    if type(shape) is not list or any(
        type(dimension) is not int or dimension < 0 for dimension in shape
    ):
        raise LauncherProtocolError(
            f"{label}.shape must be a non-negative integer array."
        )


def _validate_frames(
    manifest: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    frames: dict[str, dict[str, Any]] = {}
    mapping_keys = manifest["mapping_keys"]
    raw_frames = manifest["frames"]
    for position, value in enumerate(raw_frames):
        frame = _exact_mapping(
            value,
            _FRAME_FIELDS,
            label=f"Return frame {position}",
        )
        frame_id = _nonempty_string(
            frame["id"],
            label=f"Return frame {position} ID",
        )
        if frame_id in frames:
            raise LauncherProtocolError("Return frame IDs must be unique.")
        expected_path = f"return/dataframes/{frame_id}.parquet"
        if frame["path"] != expected_path:
            raise LauncherProtocolError(
                "Return frame path must match its stable frame ID."
            )
        _digest(
            frame["logical_digest"],
            label=f"Return frame {frame_id} logical digest",
        )
        _digest(
            frame["transport_digest"],
            label=f"Return frame {frame_id} transport digest",
        )
        if type(frame["logical_schema"]) is not list:
            raise LauncherProtocolError(
                f"Return frame {frame_id} logical schema must be an array."
            )
        if not isinstance(frame["index"], Mapping):
            raise LauncherProtocolError(
                f"Return frame {frame_id} index metadata must be an object."
            )
        if type(frame["path_cells"]) is not list:
            raise LauncherProtocolError(
                f"Return frame {frame_id} path cells must be an array."
            )
        if manifest["shape"] == "single":
            if frame["mapping_key"] is not None:
                raise LauncherProtocolError(
                    "A single return frame cannot have a mapping key."
                )
        elif (
            position >= len(mapping_keys)
            or frame["mapping_key"] != mapping_keys[position]
        ):
            raise LauncherProtocolError(
                "Return frame order must match mapping_keys."
            )
        frames[frame_id] = frame
    if manifest["shape"] == "single" and len(frames) != 1:
        raise LauncherProtocolError(
            "A single return must contain exactly one frame."
        )
    if manifest["shape"] == "mapping" and len(frames) != len(mapping_keys):
        raise LauncherProtocolError(
            "Mapping return frames must match mapping_keys."
        )
    return frames


def _validate_root_outputs(manifest: Mapping[str, Any]) -> None:
    ids: set[str] = set()
    names: set[str] = set()
    for position, value in enumerate(manifest["root_outputs"]):
        output = _exact_mapping(
            value,
            _ROOT_OUTPUT_FIELDS,
            label=f"Root output {position}",
        )
        port_id = _nonempty_string(
            output["port_id"],
            label=f"Root output {position} port ID",
        )
        name = _nonempty_string(
            output["name"],
            label=f"Root output {position} name",
        )
        if port_id in ids or name in names:
            raise LauncherProtocolError(
                "Root output IDs and names must be unique."
            )
        ids.add(port_id)
        names.add(name)


def _validate_locator(
    value: Any,
    *,
    position: int,
    frames: Mapping[str, Mapping[str, Any]],
) -> tuple[str, int, str]:
    label = f"Return locator {position}"
    locator = _exact_mapping(value, _LOCATOR_FIELDS, label=label)
    frame_id = _nonempty_string(
        locator["frame_id"],
        label=f"{label} frame ID",
    )
    if frame_id not in frames:
        raise LauncherProtocolError(f"{label} names an unknown frame.")
    if locator["mapping_key"] != frames[frame_id]["mapping_key"]:
        raise LauncherProtocolError(f"{label} mapping key is inconsistent.")
    row_position = locator["row_position"]
    if type(row_position) is not int or row_position < 0:
        raise LauncherProtocolError(
            f"{label} row position must be a non-negative integer."
        )
    _nonempty_string(locator["row_index"], label=f"{label} row index")
    column = _nonempty_string(locator["column"], label=f"{label} column")
    kind = locator["kind"]
    _shared_array(locator["shared_array"], label=f"{label} shared array")

    if kind == "record_asset":
        _nonempty_string(locator["result_key"], label=f"{label} result key")
        _nonempty_string(locator["record_id"], label=f"{label} record ID")
        _relative_path(
            locator["asset_path"],
            prefix="assets/",
            label=f"{label} record asset path",
        )
        if locator["path"] is not None:
            raise LauncherProtocolError(f"{label} has conflicting path fields.")
        if locator["asset_type"] not in {"file", "directory"}:
            raise LauncherProtocolError(f"{label} has an invalid asset type.")
        if (
            locator["shared_array"] is not None
            and locator["asset_type"] != "file"
        ):
            raise LauncherProtocolError(
                f"{label} shared array must use a file asset."
            )
        _digest(locator["digest"], label=f"{label} digest")
    elif kind == "return_asset":
        for field in ("result_key", "record_id", "asset_path"):
            if locator[field] is not None:
                raise LauncherProtocolError(
                    f"{label} has a forbidden {field} value."
                )
        _relative_path(
            locator["path"],
            prefix="return/assets/",
            label=f"{label} return asset path",
        )
        if locator["asset_type"] not in {"file", "directory"}:
            raise LauncherProtocolError(f"{label} has an invalid asset type.")
        if (
            locator["shared_array"] is not None
            and locator["asset_type"] != "file"
        ):
            raise LauncherProtocolError(
                f"{label} shared array must use a file asset."
            )
        _digest(locator["digest"], label=f"{label} digest")
    elif kind == "external_reference":
        for field in ("result_key", "record_id", "asset_path", "digest"):
            if locator[field] is not None:
                raise LauncherProtocolError(
                    f"{label} has a forbidden {field} value."
                )
        path = Path(_nonempty_string(locator["path"], label=f"{label} path"))
        if (
            not path.is_absolute()
            or path.resolve(strict=False).as_posix() != locator["path"]
            or locator["asset_type"] != "external"
            or locator["shared_array"] is not None
        ):
            raise LauncherProtocolError(
                f"{label} has an invalid external reference."
            )
    else:
        raise LauncherProtocolError(f"{label} has an unknown kind.")
    return frame_id, row_position, column


def validate_return_manifest_structure(
    value: Any,
) -> dict[str, Any]:
    """Validate all nested return-v1 fields and correlations."""
    manifest = validate_return(value)
    frames = _validate_frames(manifest)
    _validate_root_outputs(manifest)
    addresses: set[tuple[str, int, str]] = set()
    for position, locator in enumerate(manifest["locators"]):
        address = _validate_locator(
            locator,
            position=position,
            frames=frames,
        )
        if address in addresses:
            raise LauncherProtocolError(
                "Return locators must address unique DataFrame cells."
            )
        addresses.add(address)
    return manifest
