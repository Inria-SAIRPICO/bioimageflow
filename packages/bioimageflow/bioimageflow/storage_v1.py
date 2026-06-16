"""V1 output/cache storage primitives.

This module is intentionally independent from workflow execution. Runtime
integration happens in a later phase.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

import pandas as pd


CACHE_SCHEMA_VERSION = "bioimageflow.cache.v1"
CURRENT_SCHEMA = "bioimageflow.cache.current.v1"
RECORD_SCHEMA = "bioimageflow.cache.record.v1"
LINK_SCHEMA = "bioimageflow.link.v1"
RUN_SCHEMA = "bioimageflow.run.v1"
RUN_NODE_RESULT_SCHEMA = "bioimageflow.run.node_result.v1"

_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_RESULT_KEY_RE = re.compile(r"^rk_[a-z2-7]{52}$")
_RECORD_ID_RE = re.compile(r"^rec_[a-z2-7]{52}$")
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RECORD_MANIFEST_FIELDS = frozenset({"schema", "result_key", "record_id", "dataframe", "outputs"})


class CacheCorruptionError(RuntimeError):
    """Raised when v1 cache metadata points to corrupt or unsafe state."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return canonical UTF-8 JSON bytes for hashing."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_token(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(canonical_json_bytes(value)).digest()
    token = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    return f"{prefix}_{token}"


def make_result_key(material: Any) -> str:
    """Create a deterministic v1 result key from canonical material."""
    return _sha256_token("rk", {"schema": CACHE_SCHEMA_VERSION, "material": material})


def result_shard_parts(result_key: str) -> tuple[str, str]:
    """Return deterministic nested shard path parts for a result key."""
    if not _RESULT_KEY_RE.fullmatch(result_key):
        raise ValueError(f"Invalid result key: {result_key!r}")
    return result_key[3:5], result_key[5:7]


def _validate_record_id(record_id: str) -> str:
    if not _RECORD_ID_RE.fullmatch(record_id):
        raise ValueError(f"Invalid record ID: {record_id!r}")
    return record_id


def _validate_sha256_digest(digest: str, *, label: str) -> str:
    if not _SHA256_DIGEST_RE.fullmatch(digest):
        raise CacheCorruptionError(f"Invalid {label} digest.")
    return digest


def _safe_segment(raw: str) -> str:
    normalized = unicodedata.normalize("NFC", raw).strip()
    if not normalized:
        normalized = "node"
    normalized = normalized.replace("/", "_").replace("\\", "_")
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", normalized)
    normalized = normalized.strip("._-") or "node"
    normalized = normalized[:64]
    if normalized.lower() in _RESERVED_NAMES:
        normalized = f"{normalized}_node"
    return normalized.lower()


def make_node_keys(names: list[str]) -> dict[str, str]:
    """Return storage-safe node keys, disambiguating normalized collisions."""
    used: dict[str, str] = {}
    result: dict[str, str] = {}
    for name in names:
        base = _safe_segment(name)
        key = base
        if key in used:
            digest = hashlib.sha1(unicodedata.normalize("NFC", name).encode()).hexdigest()[:8]
            key = f"{base}-{digest}"
            counter = 2
            while key in used:
                key = f"{base}-{digest}-{counter}"
                counter += 1
        used[key] = name
        result[name] = key
    return result


def validate_relative_posix_path(path: str) -> str:
    """Validate and normalize a record-relative POSIX path."""
    if "\x00" in path:
        raise ValueError("Path contains NUL byte.")
    if path == "" or path.startswith("/") or "\\" in path:
        raise ValueError(f"Unsafe relative path: {path!r}")
    parts = path.split("/")
    for part in parts:
        if part in {"", ".", ".."}:
            raise ValueError(f"Unsafe relative path: {path!r}")
        if part.lower() in _RESERVED_NAMES:
            raise ValueError(f"Reserved path segment: {part!r}")
    return "/".join(parts)


def _validate_path_segment(value: str, *, label: str) -> str:
    segment = validate_relative_posix_path(value)
    if "/" in segment:
        raise ValueError(f"{label} must be one path segment: {value!r}")
    return segment


def _validate_node_key(value: str) -> str:
    return validate_relative_posix_path(value)


def _atomic_write_json(path: Path, payload: dict[str, Any], *, stem: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{stem}.{uuid.uuid4().hex}.tmp"
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp_path, path)


def _bioimageflow_version() -> str | None:
    try:
        return version("bioimageflow")
    except PackageNotFoundError:
        return None


def _is_missing(value: Any) -> bool:
    if isinstance(value, (str, bytes, Path)):
        return False
    try:
        missing = pd.isna(value)
    except TypeError:
        return False
    if hasattr(missing, "item"):
        try:
            missing = missing.item()
        except ValueError:
            return False
    try:
        return bool(missing)
    except (TypeError, ValueError):
        return False


def _datetime_payload(value: datetime | pd.Timestamp) -> dict[str, str]:
    timestamp = cast(pd.Timestamp, pd.Timestamp(value))
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(timezone.utc)
    else:
        timestamp = timestamp.tz_convert(timezone.utc)
    return {"kind": "datetime", "value": timestamp.isoformat().replace("+00:00", "Z")}


def _cell_payload(value: Any, *, column_kind: str = "scalar", dtype: str = "") -> Any:
    if hasattr(value, "item"):
        value = value.item()
    if _is_missing(value):
        return {"kind": "null", "value": None}
    if isinstance(value, (datetime, pd.Timestamp)):
        return _datetime_payload(value)
    if column_kind == "record_asset":
        return {
            "kind": "record_asset",
            "value": validate_relative_posix_path(Path(value).as_posix()),
        }
    if column_kind == "external_path":
        path = Path(value).as_posix() if isinstance(value, Path) else str(value)
        if path == "":
            raise TypeError("Unsupported dataframe value: empty external path")
        return {"kind": "external_path", "value": path}
    if isinstance(value, bool):
        return {"kind": "bool", "value": value}
    if isinstance(value, int):
        kind = "unsigned_integer" if dtype.startswith("uint") else "signed_integer"
        return {"kind": kind, "value": str(value)}
    if isinstance(value, float):
        if math.isnan(value):
            encoded = "NaN"
        elif math.isinf(value):
            encoded = "Infinity" if value > 0 else "-Infinity"
        else:
            encoded = repr(value)
        return {"kind": "float", "value": encoded}
    if isinstance(value, str):
        return {"kind": "string", "value": unicodedata.normalize("NFC", value)}
    if isinstance(value, Path):
        return {"kind": "external_path", "value": Path(value).as_posix()}
    raise TypeError(f"Unsupported dataframe value: {type(value).__name__}")


def _ordered_dataframe_columns(
    df: pd.DataFrame,
    declared_columns: Sequence[str] | None,
) -> dict[str, Any]:
    column_by_name: dict[str, Any] = {}
    for column in df.columns:
        name = str(column)
        if name in column_by_name:
            raise ValueError(f"Duplicate dataframe column after string conversion: {name!r}")
        column_by_name[name] = column
    declared = [str(column) for column in declared_columns or ()]
    missing = [column for column in declared if column not in column_by_name]
    if missing:
        raise ValueError(f"Declared dataframe columns are missing: {missing!r}")
    additional = sorted(column for column in column_by_name if column not in declared)
    return {name: column_by_name[name] for name in [*declared, *additional]}


def canonical_dataframe_digest(
    df: pd.DataFrame,
    *,
    declared_columns: Sequence[str] | None = None,
    column_kinds: Mapping[str, str] | None = None,
) -> str:
    """Return a stable digest for the supported v1 dataframe surface."""
    columns = _ordered_dataframe_columns(df, declared_columns)
    kinds = {str(key): value for key, value in (column_kinds or {}).items()}
    column_schema = [
        {
            "name": name,
            "dtype": str(df[original].dtype),
            "kind": kinds.get(name, "scalar"),
        }
        for name, original in columns.items()
    ]
    rows: list[dict[str, Any]] = []
    for index, row in df.iterrows():
        rows.append(
            {
                "index": str(index),
                "values": {
                    name: _cell_payload(
                        row[original],
                        column_kind=kinds.get(name, "scalar"),
                        dtype=str(df[original].dtype),
                    )
                    for name, original in columns.items()
                },
            }
        )
    payload = {"columns": column_schema, "rows": rows}
    return f"sha256:{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def asset_digest_and_size(path: Path) -> tuple[int, str]:
    """Return deterministic size and digest metadata for a file or directory asset."""
    if path.is_symlink():
        raise CacheCorruptionError("Asset must not be a symlink.")
    if path.is_file():
        return path.stat().st_size, _file_sha256(path)
    if not path.is_dir():
        raise CacheCorruptionError(f"Asset is not a regular file or directory: {path}")

    root = path.resolve()
    total_size = 0
    entries: list[dict[str, Any]] = []
    for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        if child.is_symlink():
            raise CacheCorruptionError(f"Directory asset contains a symlink: {child}")
        try:
            child.resolve().relative_to(root)
        except ValueError as exc:
            raise CacheCorruptionError(f"Directory asset escapes its root: {child}") from exc
        relative = validate_relative_posix_path(child.relative_to(path).as_posix())
        if child.is_dir():
            entries.append({"kind": "directory", "path": relative})
            continue
        if not child.is_file():
            raise CacheCorruptionError(f"Directory asset contains an unsupported entry: {child}")
        size = child.stat().st_size
        total_size += size
        entries.append(
            {
                "digest": _file_sha256(child),
                "kind": "file",
                "path": relative,
                "size": size,
            }
        )
    digest = hashlib.sha256(
        canonical_json_bytes({"kind": "directory", "entries": entries})
    ).hexdigest()
    return total_size, f"sha256:{digest}"


def make_record_id(manifest_material: dict[str, Any]) -> str:
    """Create a record ID from content material, excluding execution metadata."""
    excluded = {
        "record_id",
        "attempt_id",
        "run_id",
        "created_at",
        "selected_at",
        "hostname",
        "pid",
        "duration",
    }
    content = {
        key: value
        for key, value in manifest_material.items()
        if key not in excluded
    }
    return _sha256_token("rec", {"schema": RECORD_SCHEMA, "content": content})


@dataclass(frozen=True)
class RecordManifest:
    """Structured manifest helper for a reusable v1 record."""

    result_key: str
    record_id: str
    dataframe_digest: str
    outputs: list[dict[str, Any]]
    schema: str = RECORD_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "result_key": self.result_key,
            "record_id": self.record_id,
            "dataframe": {
                "path": "dataframe.parquet",
                "digest": self.dataframe_digest,
            },
            "outputs": list(self.outputs),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RecordManifest":
        if not isinstance(value, dict):
            raise CacheCorruptionError("Record manifest must be a JSON object.")
        unknown = set(value) - _RECORD_MANIFEST_FIELDS
        if unknown:
            raise CacheCorruptionError(f"Record manifest contains unknown fields: {sorted(unknown)!r}")
        if value.get("schema") != RECORD_SCHEMA:
            raise CacheCorruptionError("Invalid record manifest schema.")
        dataframe = value.get("dataframe")
        if not isinstance(dataframe, dict):
            raise CacheCorruptionError("Record manifest is missing dataframe metadata.")
        if dataframe.get("path") != "dataframe.parquet":
            raise CacheCorruptionError("Record manifest has an invalid dataframe path.")
        outputs = value.get("outputs")
        if not isinstance(outputs, list):
            raise CacheCorruptionError("Record manifest outputs must be a list.")
        try:
            return cls(
                result_key=str(value["result_key"]),
                record_id=str(value["record_id"]),
                dataframe_digest=str(dataframe["digest"]),
                outputs=[dict(output) for output in outputs],
                schema=str(value["schema"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CacheCorruptionError("Invalid record manifest shape.") from exc

    def validate(self, record_dir: Path, *, expected_result_key: str | None = None) -> None:
        if self.schema != RECORD_SCHEMA:
            raise CacheCorruptionError("Invalid record manifest schema.")
        try:
            result_shard_parts(self.result_key)
            _validate_record_id(self.record_id)
        except ValueError as exc:
            raise CacheCorruptionError("Record manifest contains unsafe identifiers.") from exc
        if expected_result_key is not None and self.result_key != expected_result_key:
            raise CacheCorruptionError("Record manifest result key mismatch.")
        if record_dir.name != self.record_id:
            raise CacheCorruptionError("Record manifest record ID mismatch.")
        _validate_sha256_digest(self.dataframe_digest, label="dataframe")
        dataframe = record_dir / "dataframe.parquet"
        if not dataframe.exists():
            raise CacheCorruptionError("Record is missing dataframe.parquet.")
        try:
            dataframe.resolve().relative_to(record_dir.resolve())
        except ValueError as exc:
            raise CacheCorruptionError("Record dataframe escapes record directory.") from exc
        if _file_sha256(dataframe) != self.dataframe_digest:
            raise CacheCorruptionError("Record dataframe digest mismatch.")
        for output in self.outputs:
            self._validate_output(record_dir, output)
        if make_record_id(self.to_dict()) != self.record_id:
            raise CacheCorruptionError("Record ID does not match record manifest content.")

    def _validate_output(self, record_dir: Path, output: dict[str, Any]) -> None:
        kind = output.get("kind")
        if kind == "owned_asset":
            try:
                relative = validate_relative_posix_path(str(output["path"]))
            except (KeyError, ValueError) as exc:
                raise CacheCorruptionError("Record manifest contains an unsafe asset path.") from exc
            asset_path = record_dir / relative
            try:
                asset_path.resolve().relative_to(record_dir.resolve())
            except ValueError as exc:
                raise CacheCorruptionError(f"Asset escapes record directory: {relative}") from exc
            if not asset_path.exists():
                raise CacheCorruptionError(f"Record asset is missing: {relative}")
            if "size" not in output or "digest" not in output:
                raise CacheCorruptionError(f"Record asset is missing size or digest: {relative}")
            asset_type = str(output.get("asset_type", "file"))
            if asset_type not in {"file", "directory"}:
                raise CacheCorruptionError(f"Record asset type is invalid: {relative}")
            if asset_type == "file" and not asset_path.is_file():
                raise CacheCorruptionError(f"Record asset is not a file: {relative}")
            if asset_type == "directory" and not asset_path.is_dir():
                raise CacheCorruptionError(f"Record asset is not a directory: {relative}")
            try:
                expected_size = int(output["size"])
            except (TypeError, ValueError) as exc:
                raise CacheCorruptionError(f"Record asset size is invalid: {relative}") from exc
            actual_size, actual_digest = asset_digest_and_size(asset_path)
            if actual_size != expected_size:
                raise CacheCorruptionError(f"Record asset size mismatch: {relative}")
            _validate_sha256_digest(str(output["digest"]), label="asset")
            if actual_digest != output["digest"]:
                raise CacheCorruptionError(f"Record asset digest mismatch: {relative}")
            if output.get("asset_role") == "shared_array":
                self._validate_shared_array_asset(asset_path, relative, output, asset_type=asset_type)
            return
        if kind == "external_path":
            if not isinstance(output.get("path"), str) or output["path"] == "":
                raise CacheCorruptionError("Record manifest external path is invalid.")
            if output.get("identity") not in {"path"}:
                raise CacheCorruptionError("Record manifest external path identity is invalid.")
            return
        raise CacheCorruptionError(f"Unsupported record output kind: {kind!r}")

    def _validate_shared_array_asset(
        self,
        asset_path: Path,
        relative: str,
        output: dict[str, Any],
        *,
        asset_type: str,
    ) -> None:
        if asset_type != "file":
            raise CacheCorruptionError(f"Record shared-array asset must be a file: {relative}")
        if not relative.startswith("assets/shm/"):
            raise CacheCorruptionError(f"Record shared-array asset path is invalid: {relative}")
        array = output.get("array")
        if not isinstance(array, dict):
            raise CacheCorruptionError(f"Record shared-array metadata is missing: {relative}")
        if not isinstance(array.get("column"), str) or array["column"] == "":
            raise CacheCorruptionError(f"Record shared-array column is invalid: {relative}")
        if not isinstance(array.get("row_index"), str):
            raise CacheCorruptionError(f"Record shared-array row index is invalid: {relative}")
        if array.get("format") != "npy":
            raise CacheCorruptionError(f"Record shared-array format is invalid: {relative}")
        if array.get("order") != "C":
            raise CacheCorruptionError(f"Record shared-array order is invalid: {relative}")
        shape = array.get("shape")
        if not isinstance(shape, list) or not all(isinstance(item, int) and item >= 0 for item in shape):
            raise CacheCorruptionError(f"Record shared-array shape is invalid: {relative}")
        dtype = array.get("dtype")
        if not isinstance(dtype, str) or dtype == "":
            raise CacheCorruptionError(f"Record shared-array dtype is invalid: {relative}")
        try:
            import numpy as np

            loaded = np.load(asset_path, allow_pickle=False)
        except Exception as exc:
            raise CacheCorruptionError(f"Record shared-array asset is unreadable: {relative}") from exc
        if list(loaded.shape) != shape:
            raise CacheCorruptionError(f"Record shared-array shape mismatch: {relative}")
        if str(loaded.dtype) != dtype:
            raise CacheCorruptionError(f"Record shared-array dtype mismatch: {relative}")
        if not loaded.flags.c_contiguous:
            raise CacheCorruptionError(f"Record shared-array order mismatch: {relative}")


@dataclass(frozen=True)
class CurrentPointer:
    """Structured helper for ``current.json``."""

    result_key: str
    record_id: str
    manifest: str
    attempt_id: str
    run_id: str
    policy: str = "first-valid"
    schema: str = CURRENT_SCHEMA
    selected_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        try:
            result_shard_parts(self.result_key)
            _validate_record_id(self.record_id)
            manifest = validate_relative_posix_path(self.manifest)
        except ValueError as exc:
            raise CacheCorruptionError("Invalid current pointer identifiers.") from exc
        return {
            "schema": self.schema,
            "result_key": self.result_key,
            "record_id": self.record_id,
            "manifest": manifest,
            "policy": self.policy,
            "selected_at": self.selected_at or datetime.now(timezone.utc).isoformat(),
            "selected_by": {
                "attempt_id": self.attempt_id,
                "run_id": self.run_id,
            },
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CurrentPointer":
        if not isinstance(value, dict):
            raise CacheCorruptionError("Current pointer must be a JSON object.")
        if value.get("schema") != CURRENT_SCHEMA:
            raise CacheCorruptionError("Invalid current pointer schema.")
        selected_by = value.get("selected_by") or {}
        if not isinstance(selected_by, dict):
            raise CacheCorruptionError("Current pointer selected_by must be an object.")
        try:
            pointer = cls(
                result_key=str(value["result_key"]),
                record_id=str(value["record_id"]),
                manifest=str(value["manifest"]),
                policy=str(value.get("policy", "first-valid")),
                attempt_id=str(selected_by.get("attempt_id", "")),
                run_id=str(selected_by.get("run_id", "")),
                selected_at=str(value.get("selected_at", "")),
            )
            pointer.to_dict()
        except (KeyError, TypeError, ValueError) as exc:
            raise CacheCorruptionError("Invalid current pointer shape.") from exc
        if pointer.policy != "first-valid":
            raise CacheCorruptionError("Invalid current pointer policy.")
        if pointer.manifest != f"records/{pointer.record_id}/manifest.json":
            raise CacheCorruptionError("Current pointer manifest path does not match record ID.")
        return pointer


class StorageV1:
    """Filesystem paths and guarded metadata updates for v1 storage."""

    def __init__(self, storage_path: str | Path) -> None:
        self.storage_path = Path(storage_path)

    @property
    def cache_root(self) -> Path:
        return self.storage_path / "cache" / "v1"

    @property
    def runs_root(self) -> Path:
        return self.storage_path / "runs"

    @property
    def latest_root(self) -> Path:
        return self.storage_path / "latest"

    def result_dir(self, result_key: str) -> Path:
        first, second = result_shard_parts(result_key)
        return self.cache_root / "results" / first / second / result_key

    def run_dir(self, run_id: str) -> Path:
        return self.runs_root / _validate_path_segment(run_id, label="Run ID")

    def run_node_dir(self, run_id: str, node_key: str) -> Path:
        return self.run_dir(run_id) / "nodes" / _validate_node_key(node_key)

    def new_attempt_id(self) -> str:
        return f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}_{uuid.uuid4().hex[:12]}"

    def write_run_metadata(
        self,
        run_id: str,
        *,
        workflow_identity: str,
        engine: str,
        status: str,
        target_nodes: Sequence[str],
        started_at: str | None = None,
        completed_at: str | None = None,
    ) -> Path:
        """Write workflow-level run metadata."""
        run_path = self.run_dir(run_id) / "run.json"
        payload = {
            "schema": RUN_SCHEMA,
            "run_id": _validate_path_segment(run_id, label="Run ID"),
            "workflow_identity": workflow_identity,
            "storage_path": str(self.storage_path),
            "started_at": started_at or datetime.now(timezone.utc).isoformat(),
            "completed_at": completed_at,
            "engine": engine,
            "bioimageflow_version": _bioimageflow_version(),
            "target_nodes": [str(node) for node in target_nodes],
            "status": status,
        }
        _atomic_write_json(run_path, payload, stem="run")
        return run_path

    def write_run_node_result(
        self,
        run_id: str,
        node_key: str,
        *,
        result_key: str,
        record_id: str,
        cache_hit: bool,
    ) -> Path:
        """Write a run-local view over the selected current record for one node."""
        safe_run_id = _validate_path_segment(run_id, label="Run ID")
        safe_node_key = _validate_node_key(node_key)
        result_shard_parts(result_key)
        record_id = _validate_record_id(record_id)
        current = self.load_current(result_key)
        if current is None or current.record_id != record_id:
            raise CacheCorruptionError("Run node result must reference the selected current record.")
        manifest = self._load_record_manifest(result_key, record_id)
        record_dir = self.result_dir(result_key) / "records" / record_id
        node_dir = self.run_node_dir(run_id, node_key)
        canonical = self._relative_target(node_dir / "result.json", record_dir)
        payload = {
            "schema": RUN_NODE_RESULT_SCHEMA,
            "run_id": safe_run_id,
            "node_key": safe_node_key,
            "result_key": result_key,
            "record_id": record_id,
            "cache_hit": bool(cache_hit),
            "canonical": canonical,
            "outputs": list(manifest.outputs),
        }
        result_path = node_dir / "result.json"
        _atomic_write_json(result_path, payload, stem="result")
        self._write_link(node_dir / "record.bioimageflow-link.json", kind="directory", target=record_dir)
        self._write_output_links(node_dir, record_dir, manifest.outputs)
        return result_path

    def update_latest_node(self, node_key: str, run_id: str) -> Path:
        """Atomically point ``latest/<node-key>`` at a run-node view."""
        target = self.run_node_dir(run_id, node_key)
        self._validate_run_node_view(run_id, node_key)
        latest_path = self._latest_node_path(node_key)
        self._write_link(latest_path, kind="directory", target=target)
        return latest_path

    def update_latest_success_run(self, run_id: str) -> Path:
        """Atomically point ``runs/latest-success`` at a successful run view."""
        target = self.run_dir(run_id)
        run = self._load_run_metadata(run_id)
        if run.get("status") != "succeeded":
            raise CacheCorruptionError("Latest successful run must reference a successful run.")
        latest_path = self.runs_root / "latest-success.bioimageflow-link.json"
        self._write_link(latest_path, kind="directory", target=target)
        return latest_path

    def load_current(self, result_key: str) -> CurrentPointer | None:
        result_dir = self.result_dir(result_key)
        path = result_dir / "current.json"
        if not path.exists():
            return None
        try:
            pointer = CurrentPointer.from_dict(json.loads(path.read_text()))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise CacheCorruptionError(f"Invalid current pointer for {result_key}") from exc
        if pointer.result_key != result_key:
            raise CacheCorruptionError("Current pointer result key mismatch.")
        try:
            manifest_path = result_dir / validate_relative_posix_path(pointer.manifest)
        except ValueError as exc:
            raise CacheCorruptionError("Current pointer manifest path is unsafe.") from exc
        try:
            manifest_path.resolve().relative_to(result_dir.resolve())
        except ValueError as exc:
            raise CacheCorruptionError("Current pointer manifest escapes result directory.") from exc
        if not manifest_path.exists():
            raise CacheCorruptionError("Current pointer references a missing manifest.")
        manifest = self._load_record_manifest(result_key, pointer.record_id)
        if manifest.record_id != pointer.record_id:
            raise CacheCorruptionError("Current pointer record ID mismatch.")
        return pointer

    def select_current_record(
        self,
        result_key: str,
        *,
        candidate_record_id: str,
        attempt_id: str,
        run_id: str,
    ) -> CurrentPointer:
        result_dir = self.result_dir(result_key)
        records_dir = result_dir / "records"
        candidate_record_id = _validate_record_id(candidate_record_id)
        self._load_record_manifest(result_key, candidate_record_id)
        candidate_manifest = records_dir / candidate_record_id / "manifest.json"
        if not candidate_manifest.exists():
            raise CacheCorruptionError("Candidate record manifest is missing.")
        result_dir.mkdir(parents=True, exist_ok=True)
        lock_dir = result_dir / ".current.lock"
        while True:
            try:
                lock_dir.mkdir()
                break
            except FileExistsError:
                # Local primitive only: fail fast instead of spinning forever.
                raise CacheCorruptionError("Current pointer is locked by another writer.")
        try:
            existing = self.load_current(result_key)
            if existing is not None:
                if existing.record_id != candidate_record_id:
                    self._write_conflict(result_key, existing.record_id, candidate_record_id, attempt_id, run_id)
                return existing
            pointer = CurrentPointer(
                result_key=result_key,
                record_id=candidate_record_id,
                manifest=f"records/{candidate_record_id}/manifest.json",
                attempt_id=attempt_id,
                run_id=run_id,
            )
            tmp_path = result_dir / f".current.{uuid.uuid4().hex}.tmp"
            tmp_path.write_text(json.dumps(pointer.to_dict(), indent=2, sort_keys=True))
            os.replace(tmp_path, result_dir / "current.json")
            return pointer
        finally:
            lock_dir.rmdir()

    def _latest_node_path(self, node_key: str) -> Path:
        safe_node_key = _validate_node_key(node_key)
        parent = self.latest_root
        parts = safe_node_key.split("/")
        for part in parts[:-1]:
            parent = parent / part
        return parent / f"{parts[-1]}.bioimageflow-link.json"

    def _relative_target(self, pointer_path: Path, target: Path) -> str:
        return os.path.relpath(target, start=pointer_path.parent).replace(os.sep, "/")

    def _write_link(
        self,
        path: Path,
        *,
        kind: str,
        target: Path,
        digest: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "schema": LINK_SCHEMA,
            "kind": kind,
            "target": self._relative_target(path, target),
        }
        if digest is not None:
            payload["digest"] = digest
        _atomic_write_json(path, payload, stem="link")

    def _write_output_links(
        self,
        node_dir: Path,
        record_dir: Path,
        outputs: list[dict[str, Any]],
    ) -> None:
        for output in outputs:
            if output.get("kind") != "owned_asset":
                continue
            try:
                relative = validate_relative_posix_path(str(output["path"]))
            except (KeyError, ValueError) as exc:
                raise CacheCorruptionError("Run output link contains an unsafe asset path.") from exc
            asset_path = record_dir / relative
            try:
                asset_path.resolve().relative_to(record_dir.resolve())
            except ValueError as exc:
                raise CacheCorruptionError(f"Run output link escapes record directory: {relative}") from exc
            if not asset_path.exists():
                raise CacheCorruptionError(f"Run output link target is missing: {relative}")
            asset_type = str(output.get("asset_type", "file"))
            if asset_type not in {"file", "directory"}:
                raise CacheCorruptionError(f"Run output link asset type is invalid: {relative}")
            link_kind = "directory" if asset_type == "directory" else "file"
            link_path = node_dir / "outputs" / f"{relative}.bioimageflow-link.json"
            digest = str(output.get("digest")) if link_kind == "file" and output.get("digest") is not None else None
            self._write_link(link_path, kind=link_kind, target=asset_path, digest=digest)

    def _load_run_metadata(self, run_id: str) -> dict[str, Any]:
        run_path = self.run_dir(run_id) / "run.json"
        if not run_path.exists():
            raise CacheCorruptionError("Run metadata is missing.")
        try:
            payload = json.loads(run_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CacheCorruptionError("Run metadata is invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise CacheCorruptionError("Run metadata must be a JSON object.")
        if payload.get("schema") != RUN_SCHEMA:
            raise CacheCorruptionError("Run metadata has an invalid schema.")
        if payload.get("run_id") != run_id:
            raise CacheCorruptionError("Run metadata run ID mismatch.")
        if not isinstance(payload.get("status"), str) or payload["status"] == "":
            raise CacheCorruptionError("Run metadata status is invalid.")
        return payload

    def _validate_run_node_view(self, run_id: str, node_key: str) -> dict[str, Any]:
        node_dir = self.run_node_dir(run_id, node_key)
        result_path = node_dir / "result.json"
        if not result_path.exists():
            raise CacheCorruptionError("Run node view is missing result.json.")
        try:
            payload = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CacheCorruptionError("Run node result is invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise CacheCorruptionError("Run node result must be a JSON object.")
        if payload.get("schema") != RUN_NODE_RESULT_SCHEMA:
            raise CacheCorruptionError("Run node result has an invalid schema.")
        if payload.get("run_id") != run_id:
            raise CacheCorruptionError("Run node result run ID mismatch.")
        if payload.get("node_key") != node_key:
            raise CacheCorruptionError("Run node result node key mismatch.")
        result_key = str(payload.get("result_key", ""))
        record_id = str(payload.get("record_id", ""))
        try:
            result_shard_parts(result_key)
            _validate_record_id(record_id)
        except ValueError as exc:
            raise CacheCorruptionError("Run node result contains invalid identifiers.") from exc
        current = self.load_current(result_key)
        if current is None or current.record_id != record_id:
            raise CacheCorruptionError("Run node result no longer references the selected current record.")
        manifest = self._load_record_manifest(result_key, record_id)
        record_dir = self.result_dir(result_key) / "records" / record_id
        expected_canonical = self._relative_target(result_path, record_dir)
        if payload.get("canonical") != expected_canonical:
            raise CacheCorruptionError("Run node result canonical path mismatch.")
        record_link = node_dir / "record.bioimageflow-link.json"
        if not record_link.exists():
            raise CacheCorruptionError("Run node record pointer is missing.")
        self._validate_link(record_link, kind="directory", target=record_dir)
        for output in manifest.outputs:
            if output.get("kind") != "owned_asset":
                continue
            relative = validate_relative_posix_path(str(output["path"]))
            asset_type = str(output.get("asset_type", "file"))
            link_kind = "directory" if asset_type == "directory" else "file"
            digest = str(output.get("digest")) if link_kind == "file" and output.get("digest") is not None else None
            self._validate_link(
                node_dir / "outputs" / f"{relative}.bioimageflow-link.json",
                kind=link_kind,
                target=record_dir / relative,
                digest=digest,
            )
        return payload

    def _validate_link(
        self,
        path: Path,
        *,
        kind: str,
        target: Path,
        digest: str | None = None,
    ) -> None:
        if not path.exists():
            raise CacheCorruptionError(f"Run view pointer is missing: {path.name}")
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CacheCorruptionError("Run view pointer is invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise CacheCorruptionError("Run view pointer must be a JSON object.")
        if payload.get("schema") != LINK_SCHEMA:
            raise CacheCorruptionError("Run view pointer has an invalid schema.")
        if payload.get("kind") != kind:
            raise CacheCorruptionError("Run view pointer kind mismatch.")
        expected_target = self._relative_target(path, target)
        if payload.get("target") != expected_target:
            raise CacheCorruptionError("Run view pointer target mismatch.")
        if digest is not None and payload.get("digest") != digest:
            raise CacheCorruptionError("Run view pointer digest mismatch.")

    def _load_record_manifest(self, result_key: str, record_id: str) -> RecordManifest:
        record_id = _validate_record_id(record_id)
        result_dir = self.result_dir(result_key)
        records_dir = result_dir / "records"
        record_dir = records_dir / record_id
        try:
            record_dir.resolve().relative_to(records_dir.resolve())
        except ValueError as exc:
            raise CacheCorruptionError("Record directory escapes records directory.") from exc
        manifest_path = record_dir / "manifest.json"
        if not manifest_path.exists():
            raise CacheCorruptionError("Record manifest is missing.")
        try:
            manifest_path.resolve().relative_to(record_dir.resolve())
        except ValueError as exc:
            raise CacheCorruptionError("Record manifest escapes record directory.") from exc
        try:
            manifest = RecordManifest.from_dict(json.loads(manifest_path.read_text()))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CacheCorruptionError("Invalid record manifest JSON.") from exc
        manifest.validate(record_dir, expected_result_key=result_key)
        return manifest

    def _write_conflict(
        self,
        result_key: str,
        current_record_id: str,
        candidate_record_id: str,
        attempt_id: str,
        run_id: str,
    ) -> None:
        conflicts_dir = self.result_dir(result_key) / "conflicts"
        conflicts_dir.mkdir(exist_ok=True)
        conflict_id = _sha256_token(
            "conflict",
            {
                "result_key": result_key,
                "current_record_id": current_record_id,
                "candidate_record_id": candidate_record_id,
                "attempt_id": attempt_id,
                "run_id": run_id,
            },
        )
        payload = {
            "schema": "bioimageflow.cache.conflict.v1",
            "result_key": result_key,
            "current_record_id": current_record_id,
            "candidate_record_id": candidate_record_id,
            "attempt_id": attempt_id,
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        (conflicts_dir / f"{conflict_id}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True)
        )
