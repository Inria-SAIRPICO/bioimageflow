"""Storage-free immutable workflow and invocation preparation."""

from __future__ import annotations

import math
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Literal

import pandas as pd

from bioimageflow.launcher.cluster_bundle import (
    _dataframe_paths_are_cluster_paths,
    _manifest,
    _safe_basename,
    _walk_upload,
)
from bioimageflow.launcher.inputs import encode_cluster_typed_constant
from bioimageflow.launcher.node_inputs import (
    contains_local_upload,
    normalize_node_input_overrides,
    validate_remote_path_value,
)
from bioimageflow.launcher.payload import serialize_workflow_payload
from bioimageflow.launcher.types import LocalUpload
from bioimageflow.parsl import ParslTaskPolicy
from bioimageflow.storage import canonical_json_bytes
from bioimageflow.storage.dataframe_transport import write_dataframe_transport
from bioimageflow.validation import is_path_type
from bioimageflow.workflow import Workflow

from ._common import DIGEST_RE, canonical_digest, exact_dict, freeze_json, thaw_json


@dataclass(frozen=True, slots=True)
class PreparedInvocationEntry:
    path: str
    kind: Literal["file", "directory"]
    size: int
    digest: str

    def __post_init__(self) -> None:
        path = PurePosixPath(self.path)
        if not self.path or path.is_absolute() or str(path) != self.path or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("Prepared invocation entry path is invalid.")
        if self.kind not in {"file", "directory"}:
            raise ValueError("Prepared invocation entry kind is invalid.")
        if type(self.size) is not int or self.size < 0:
            raise ValueError("Prepared invocation entry size is invalid.")
        if DIGEST_RE.fullmatch(self.digest) is None:
            raise ValueError("Prepared invocation entry digest is invalid.")

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "size": self.size, "digest": self.digest}

    @classmethod
    def from_dict(cls, value: Any) -> "PreparedInvocationEntry":
        data = exact_dict(value, {"path", "kind", "size", "digest"}, cls.__name__)
        return cls(**data)


@dataclass(frozen=True, slots=True)
class PreparedInvocationManifest:
    SCHEMA: ClassVar[str] = "bioimageflow.prepared_cluster_invocation.v1"
    invocation_digest: str
    workflow_digest: str
    entries: tuple[PreparedInvocationEntry, ...]
    inputs: tuple[Mapping[str, Any], ...]
    targets: tuple[str, ...] | None
    node_input_overrides: tuple[Mapping[str, Any], ...]
    task_policy: ParslTaskPolicy

    def __post_init__(self) -> None:
        if DIGEST_RE.fullmatch(self.invocation_digest) is None or DIGEST_RE.fullmatch(self.workflow_digest) is None:
            raise ValueError("Prepared invocation digests are invalid.")
        if any(type(entry) is not PreparedInvocationEntry for entry in self.entries):
            raise TypeError("Prepared invocation entries must be PreparedInvocationEntry values.")
        frozen_inputs = freeze_json(
            self.inputs,
            path="inputs",
            reject_sensitive_keys=False,
        )
        frozen_overrides = freeze_json(
            self.node_input_overrides,
            path="node_input_overrides",
            reject_sensitive_keys=False,
        )
        if not isinstance(frozen_inputs, tuple) or any(
            not isinstance(item, Mapping) for item in frozen_inputs
        ):
            raise TypeError("Prepared invocation inputs must contain mappings.")
        if not isinstance(frozen_overrides, tuple) or any(
            not isinstance(item, Mapping) for item in frozen_overrides
        ):
            raise TypeError("Prepared invocation node overrides must contain mappings.")
        object.__setattr__(self, "inputs", frozen_inputs)
        object.__setattr__(self, "node_input_overrides", frozen_overrides)
        if self.targets is not None:
            targets = tuple(self.targets)
            if any(type(target) is not str or not target for target in targets):
                raise ValueError("Prepared invocation targets must be non-empty strings.")
            object.__setattr__(self, "targets", targets)
        if type(self.task_policy) is not ParslTaskPolicy:
            raise TypeError("task_policy must be ParslTaskPolicy.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "invocation_digest": self.invocation_digest,
            "workflow_digest": self.workflow_digest,
            "entries": [entry.to_dict() for entry in self.entries],
            "inputs": thaw_json(self.inputs),
            "targets": None if self.targets is None else list(self.targets),
            "node_input_overrides": thaw_json(self.node_input_overrides),
            "task_policy": self.task_policy.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "PreparedInvocationManifest":
        data = exact_dict(value, {"schema", "invocation_digest", "workflow_digest", "entries", "inputs", "targets", "node_input_overrides", "task_policy"}, cls.__name__)
        if data["schema"] != cls.SCHEMA:
            raise ValueError("Unsupported PreparedClusterInvocation schema.")
        return cls(
            invocation_digest=data["invocation_digest"],
            workflow_digest=data["workflow_digest"],
            entries=tuple(PreparedInvocationEntry.from_dict(item) for item in data["entries"]),
            inputs=tuple(data["inputs"]),
            targets=None if data["targets"] is None else tuple(data["targets"]),
            node_input_overrides=tuple(data["node_input_overrides"]),
            task_policy=ParslTaskPolicy.from_dict(data["task_policy"]),
        )


def _cluster_path(value: Path, *, field: str) -> str:
    text = value.as_posix()
    path = PurePosixPath(text)
    if not path.is_absolute() or text.startswith("//") or str(path) != text or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise ValueError(f"{field} Path values must be normalized absolute cluster paths or LocalUpload values.")
    return text


def _encode_path_value(value: Any, root: Path, next_upload: list[int]) -> dict[str, Any]:
    if value is None:
        return {"tag": "none", "value": None}
    if isinstance(value, LocalUpload):
        position = next_upload[0]
        next_upload[0] += 1
        relative = f"uploads/{position}"
        kind, tree = _walk_upload(value.path, root / relative)
        return {"tag": "local_upload", "upload_path": relative, "root_kind": kind, "root_name": _safe_basename(value.path), "tree": tree}
    if isinstance(value, Path):
        return {"tag": "cluster_path", "value": _cluster_path(value, field="input")}
    if type(value) in {list, tuple}:
        return {"tag": "list" if type(value) is list else "tuple", "value": [_encode_path_value(item, root, next_upload) for item in value]}
    raise TypeError("Unsupported remote path value.")


class PreparedClusterInvocation:
    """Owned immutable local bytes for one future remote execution plan."""

    def __init__(self, *, temporary: tempfile.TemporaryDirectory[str] | None, root: Path | None, manifest: PreparedInvocationManifest, expires_at: float | None, workflow: Workflow | None = None) -> None:
        self._temporary = temporary
        self._root = root
        self.manifest = manifest
        self._expires_at = expires_at
        self._workflow = workflow
        self._closed = False
        self._lease: str | None = None
        self._lock = threading.RLock()

    @property
    def invocation_digest(self) -> str:
        return self.manifest.invocation_digest

    @property
    def expired(self) -> bool:
        return self._expires_at is not None and time.monotonic() >= self._expires_at

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def detached(self) -> bool:
        return self._root is None

    @property
    def root(self) -> Path:
        self._verify()
        assert self._root is not None
        return self._root

    def _verify(self) -> None:
        if self._closed or self._root is None:
            raise RuntimeError("local-state-unavailable")
        if self.expired and self._lease is None:
            self.close()
            raise RuntimeError("Prepared cluster invocation expired.")
        observed = _manifest(self._root)
        expected = [entry.to_dict() for entry in self.manifest.entries]
        if observed["entries"] != expected:
            raise RuntimeError("Prepared invocation bytes no longer match their manifest.")

    def _acquire_lease(self, lease_id: str) -> None:
        with self._lock:
            self._verify()
            if self._lease is not None:
                raise RuntimeError("Prepared invocation already has an active plan.")
            self._lease = lease_id

    def _release_lease(self, lease_id: str) -> None:
        with self._lock:
            if self._lease == lease_id:
                self._lease = None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._lease is not None:
                raise RuntimeError("Prepared invocation is leased by an active plan.")
            self._closed = True
            if self._temporary is not None:
                self._temporary.cleanup()
            self._temporary = None
            self._root = None

    def to_dict(self) -> dict[str, Any]:
        return self.manifest.to_dict()

    @classmethod
    def from_dict(cls, value: Any) -> "PreparedClusterInvocation":
        return cls(temporary=None, root=None, manifest=PreparedInvocationManifest.from_dict(value), expires_at=None)

    def __enter__(self) -> "PreparedClusterInvocation":
        if self._closed:
            raise RuntimeError("Prepared invocation is closed.")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def prepare_cluster_invocation(
    workflow: Workflow,
    *,
    inputs: Mapping[str, Any] | None = None,
    targets: Sequence[str] | None = None,
    node_input_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    task_policy: ParslTaskPolicy | None = None,
    lifetime: float = 3600,
) -> PreparedClusterInvocation:
    """Snapshot a recursive workflow and invocation without network operations."""
    if not isinstance(workflow, Workflow):
        raise TypeError("workflow must be Workflow.")
    if inputs is not None and targets is not None:
        raise ValueError("inputs and targets are mutually exclusive.")
    if (
        type(lifetime) not in {int, float}
        or not math.isfinite(float(lifetime))
        or lifetime <= 0
    ):
        raise ValueError("lifetime must be a positive finite number of seconds.")
    selected_policy = task_policy or ParslTaskPolicy()
    if type(selected_policy) is not ParslTaskPolicy:
        raise TypeError("task_policy must be ParslTaskPolicy or None.")
    temporary = tempfile.TemporaryDirectory(prefix="bioimageflow-cluster-invocation-")
    root = Path(temporary.name)
    try:
        workflow_payload = serialize_workflow_payload(workflow)
        supplied = dict(inputs or {})
        ports = {port.name: port for port in workflow._interface_inputs.values()}
        if unknown := set(supplied) - set(ports):
            raise ValueError(f"Unknown workflow input(s): {sorted(unknown)}.")
        encoded_inputs: list[dict[str, Any]] = []
        next_upload = [0]
        for position, (name, value) in enumerate(supplied.items()):
            port = ports[name]
            if isinstance(value, LocalUpload):
                if port.kind != "field" or not is_path_type(port.annotation):
                    raise TypeError(f"LocalUpload is not allowed for workflow input {name!r}.")
                encoded_inputs.append({"name": name, "kind": "remote_path_value", "value": _encode_path_value(value, root, next_upload)})
            elif contains_local_upload(value):
                if port.kind != "field":
                    raise TypeError(f"Nested LocalUpload is not allowed for workflow input {name!r}.")
                validate_remote_path_value(port.annotation, value)
                encoded_inputs.append({"name": name, "kind": "remote_path_value", "value": _encode_path_value(value, root, next_upload)})
            elif port.kind == "dataframe":
                if not isinstance(value, pd.DataFrame):
                    raise TypeError(f"DataFrame input {name!r} requires a DataFrame.")
                _dataframe_paths_are_cluster_paths(value)
                relative = f"dataframes/{position}.parquet"
                metadata = write_dataframe_transport(value, root / relative, preserve_paths=True)
                encoded_inputs.append({"name": name, "kind": "dataframe", "path": relative, "metadata": metadata})
            else:
                if isinstance(value, Path):
                    _cluster_path(value, field=name)
                encoded_inputs.append({"name": name, "kind": "constant", "value": encode_cluster_typed_constant(value)})
        normalized_overrides = normalize_node_input_overrides(workflow, node_input_overrides)
        encoded_overrides = tuple(
            {"scoped_node_path": scoped, "input_name": name, "value": _encode_path_value(value, root, next_upload)}
            for scoped, name, value in normalized_overrides
        )
        invocation = {
            "workflow": workflow_payload,
            "inputs": encoded_inputs,
            "targets": None if targets is None else list(targets),
            "node_input_overrides": list(encoded_overrides),
            "task_policy": selected_policy.to_dict(),
        }
        (root / "invocation.json").write_bytes(canonical_json_bytes(invocation))
        bundle_manifest = _manifest(root)
        entries = tuple(PreparedInvocationEntry.from_dict(item) for item in bundle_manifest["entries"])
        identity = {
            "workflow_digest": workflow_payload["digest"],
            "inputs": encoded_inputs,
            "targets": invocation["targets"],
            "node_input_overrides": list(encoded_overrides),
            "task_policy": selected_policy.to_dict(),
            "entries": [entry.to_dict() for entry in entries],
        }
        manifest = PreparedInvocationManifest(
            invocation_digest=canonical_digest(identity),
            workflow_digest=workflow_payload["digest"],
            entries=entries,
            inputs=tuple(encoded_inputs),
            targets=None if targets is None else tuple(targets),
            node_input_overrides=encoded_overrides,
            task_policy=selected_policy,
        )
        return PreparedClusterInvocation(
            temporary=temporary,
            root=root,
            manifest=manifest,
            expires_at=time.monotonic() + float(lifetime),
            workflow=workflow._snapshot_definition(),
        )
    except BaseException:
        temporary.cleanup()
        raise


__all__ = [
    "PreparedClusterInvocation",
    "PreparedInvocationEntry",
    "PreparedInvocationManifest",
    "prepare_cluster_invocation",
]
