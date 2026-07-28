"""Canonical, integrity-checked Parquet transport for DataFrames."""

from __future__ import annotations

import hashlib
import math
import os
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pandas as pd

from .identity import canonical_dataframe_identity


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_METADATA_KEYS = {
    "index",
    "logical_digest",
    "logical_schema",
    "path_cells",
    "transport_digest",
}


def file_sha256(path: Path) -> str:
    """Return the SHA-256 digest of one regular file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _normalize_path(value: Path) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve(strict=False)


def _logical_frame_and_path_cells(
    frame: pd.DataFrame,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    logical = frame.copy(deep=True)
    names: dict[str, int] = {}
    for position, column in enumerate(logical.columns):
        name = str(column)
        if name in names:
            raise ValueError(
                f"Duplicate dataframe column after string conversion: {name!r}"
            )
        names[name] = position

    path_cells: list[dict[str, Any]] = []
    for row_position in range(len(logical)):
        for name, column_position in names.items():
            value = logical.iat[row_position, column_position]
            if not isinstance(value, Path):
                continue
            logical.iat[row_position, column_position] = _normalize_path(value)
            path_cells.append({"column": name, "row_position": row_position})
    return logical, path_cells


def _is_missing(value: Any) -> bool:
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


def _prepare_for_parquet(frame: pd.DataFrame) -> pd.DataFrame:
    prepared = frame.copy(deep=True)
    for column in prepared.columns:
        series = prepared[column]
        if isinstance(series.dtype, pd.CategoricalDtype):
            continue
        if series.dtype != object and not pd.api.types.is_string_dtype(series):
            continue
        normalized: list[Any] = []
        for value in series:
            if isinstance(value, Path):
                normalized.append(_normalize_path(value).as_posix())
                continue
            if type(value) in {str, int, float, bool, type(None)}:
                normalized.append(value)
                continue
            if _is_missing(value):
                normalized.append(value)
                continue
            if hasattr(value, "item"):
                scalar = value.item()
                if type(scalar) in {str, int, float, bool, type(None)}:
                    normalized.append(scalar)
                    continue
            raise TypeError(
                f"Unsupported dataframe value in column {str(column)!r}: "
                f"{type(value).__name__}"
            )
        prepared[column] = normalized
    return prepared


def _write_parquet(frame: pd.DataFrame, path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.Table.from_pandas(_prepare_for_parquet(frame), preserve_index=True)
    pq.write_table(
        table,
        path,
        version="2.6",
        data_page_version="1.0",
        compression="zstd",
        compression_level=3,
        use_dictionary=False,
        write_statistics=False,
        write_page_index=False,
        coerce_timestamps="us",
        allow_truncated_timestamps=False,
        store_schema=True,
        use_compliant_nested_type=True,
        row_group_size=65_536,
    )


def _index_metadata(index: pd.Index) -> dict[str, Any]:
    if isinstance(index, pd.MultiIndex):
        names = [None if name is None else str(name) for name in index.names]
        dtypes = [
            str(index.get_level_values(position).dtype)
            for position in range(index.nlevels)
        ]
        kind = "multi_index"
    else:
        names = [None if index.name is None else str(index.name)]
        dtypes = [str(index.dtype)]
        kind = "index"
    return {
        "dtypes": dtypes,
        "kind": kind,
        "length": len(index),
        "names": names,
    }


def write_dataframe_transport(
    frame: pd.DataFrame,
    destination: Path,
) -> dict[str, Any]:
    """Write one canonical Parquet file and return its verification metadata."""
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame must be a pandas DataFrame.")
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"DataFrame transport already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    logical, path_cells = _logical_frame_and_path_cells(frame)
    logical_schema, logical_digest = canonical_dataframe_identity(logical)
    temporary = destination.parent / (f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_parquet(logical, temporary)
        transport_index = pd.read_parquet(temporary).index
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()

    return {
        "index": _index_metadata(transport_index),
        "logical_digest": logical_digest,
        "logical_schema": logical_schema,
        "path_cells": path_cells,
        "transport_digest": file_sha256(destination),
    }


def _require_plain_dict(
    value: Any,
    *,
    keys: set[str],
    label: str,
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ValueError(f"{label} must contain exactly {sorted(keys)}.")
    return value


def _validate_digest(value: Any, *, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _validate_index_metadata(value: Any) -> dict[str, Any]:
    metadata = _require_plain_dict(
        value,
        keys={"dtypes", "kind", "length", "names"},
        label="DataFrame index metadata",
    )
    if metadata["kind"] not in {"index", "multi_index"}:
        raise ValueError("DataFrame index kind is invalid.")
    if type(metadata["length"]) is not int or metadata["length"] < 0:
        raise ValueError("DataFrame index length must be a non-negative integer.")
    names = metadata["names"]
    dtypes = metadata["dtypes"]
    if (
        type(names) is not list
        or not names
        or any(name is not None and type(name) is not str for name in names)
        or type(dtypes) is not list
        or len(dtypes) != len(names)
        or any(type(dtype) is not str or not dtype for dtype in dtypes)
    ):
        raise ValueError("DataFrame index names or dtypes are invalid.")
    expected_levels = 1 if metadata["kind"] == "index" else len(names)
    if len(names) != expected_levels:
        raise ValueError("DataFrame index level count is invalid.")
    return metadata


def _validate_path_cells(
    value: Any,
    *,
    row_count: int,
    columns: Mapping[str, int],
) -> list[tuple[int, int]]:
    if type(value) is not list:
        raise ValueError("DataFrame path_cells must be a list.")
    result: list[tuple[int, int]] = []
    seen: set[tuple[int, str]] = set()
    for item in value:
        entry = _require_plain_dict(
            item,
            keys={"column", "row_position"},
            label="DataFrame path cell",
        )
        column = entry["column"]
        position = entry["row_position"]
        if type(column) is not str or column not in columns:
            raise ValueError("DataFrame path cell names an unknown column.")
        if type(position) is not int or not 0 <= position < row_count:
            raise ValueError("DataFrame path cell row position is out of range.")
        key = (position, column)
        if key in seen:
            raise ValueError("DataFrame path cell entries must be unique.")
        seen.add(key)
        result.append((position, columns[column]))
    return result


def _validate_json_value(value: Any, *, label: str) -> None:
    if value is None or type(value) in {bool, int, str}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite float.")
        return
    if type(value) is list:
        for item in value:
            _validate_json_value(item, label=label)
        return
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError(f"{label} contains a non-string key.")
        for item in value.values():
            _validate_json_value(item, label=label)
        return
    raise ValueError(f"{label} contains a non-JSON value.")


def read_dataframe_transport(
    source: Path,
    metadata: Mapping[str, Any],
) -> pd.DataFrame:
    """Verify and load one canonical DataFrame transport artifact."""
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise ValueError("DataFrame transport must be a regular non-symlink file.")
    raw_metadata = _require_plain_dict(
        metadata,
        keys=_METADATA_KEYS,
        label="DataFrame transport metadata",
    )
    logical_digest = _validate_digest(
        raw_metadata["logical_digest"],
        label="DataFrame logical digest",
    )
    transport_digest = _validate_digest(
        raw_metadata["transport_digest"],
        label="DataFrame transport digest",
    )
    if file_sha256(source) != transport_digest:
        raise ValueError("DataFrame transport digest does not match.")

    logical_schema = raw_metadata["logical_schema"]
    if type(logical_schema) is not list:
        raise ValueError("DataFrame logical schema must be a list.")
    _validate_json_value(logical_schema, label="DataFrame logical schema")
    expected_index = _validate_index_metadata(raw_metadata["index"])

    frame = pd.read_parquet(source)
    if _index_metadata(frame.index) != expected_index:
        raise ValueError("DataFrame index metadata does not match.")
    columns: dict[str, int] = {}
    for position, column in enumerate(frame.columns):
        name = str(column)
        if name in columns:
            raise ValueError(
                f"Duplicate dataframe column after string conversion: {name!r}"
            )
        columns[name] = position
    path_cells = _validate_path_cells(
        raw_metadata["path_cells"],
        row_count=len(frame),
        columns=columns,
    )
    for row_position, column_position in path_cells:
        value = frame.iat[row_position, column_position]
        if type(value) is not str or not value:
            raise ValueError("DataFrame path cell transport value is not a string.")
        path = Path(value)
        if not path.is_absolute() or _normalize_path(path).as_posix() != value:
            raise ValueError("DataFrame path cell is not a normalized absolute path.")
        frame.iat[row_position, column_position] = path

    actual_schema, actual_digest = canonical_dataframe_identity(frame)
    if actual_schema != logical_schema or actual_digest != logical_digest:
        raise ValueError("DataFrame logical schema or digest does not match.")
    return frame
