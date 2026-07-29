"""Strict submitted-invocation and root-input transport."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal, NoReturn, TYPE_CHECKING, cast

import pandas as pd

from bioimageflow.storage import validate_relative_posix_path
from bioimageflow.storage.dataframe_transport import (
    read_dataframe_transport,
    write_dataframe_transport,
)

from .errors import LauncherProtocolError

if TYPE_CHECKING:
    from bioimageflow.workflow import Workflow


INVOCATION_SCHEMA = "bioimageflow.launcher.invocation.v1"
_MAX_CONSTANT_DEPTH = 64
_MAX_CONSTANT_NODES = 100_000
_CONSTANT_TAGS = {
    "bool",
    "dict",
    "float",
    "int",
    "list",
    "none",
    "path",
    "str",
    "tuple",
}
_DATAFRAME_METADATA_KEYS = {
    "index",
    "logical_digest",
    "logical_schema",
    "path",
    "path_cells",
    "transport_digest",
}


@dataclass(frozen=True, slots=True)
class InvocationOutput:
    """One stable published root output address."""

    port_id: str
    name: str


@dataclass(frozen=True, slots=True)
class LoadedInvocation:
    """A verified invocation ready to execute before engine acquisition."""

    variant: Literal["root", "targets"]
    inputs: Mapping[str, Any]
    targets: tuple[str, ...]
    outputs: tuple[InvocationOutput, ...]


@dataclass(slots=True)
class _CodecState:
    active: set[int]
    nodes: int = 0

    def visit(self, *, depth: int) -> None:
        if depth > _MAX_CONSTANT_DEPTH:
            raise ValueError("Constant nesting exceeds the supported depth.")
        self.nodes += 1
        if self.nodes > _MAX_CONSTANT_NODES:
            raise ValueError("Constant contains too many values.")


def _normalized_absolute_path(value: Path) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve(strict=False)


def _encode_constant(
    value: Any,
    *,
    state: _CodecState,
    depth: int,
    preserve_paths: bool,
) -> dict[str, Any]:
    state.visit(depth=depth)
    value_type = type(value)
    if value is None:
        return {"tag": "none", "value": None}
    if value_type is bool:
        return {"tag": "bool", "value": value}
    if value_type is int:
        return {"tag": "int", "value": value}
    if value_type is float:
        if not math.isfinite(value):
            raise ValueError("Constants must not contain non-finite floats.")
        return {"tag": "float", "value": value}
    if value_type is str:
        return {"tag": "str", "value": value}
    if isinstance(value, Path):
        if preserve_paths:
            encoded = value.as_posix()
            candidate = Path(encoded)
            if (
                not candidate.is_absolute()
                or encoded.startswith("//")
                or candidate.as_posix() != encoded
                or any(part in {"", ".", ".."} for part in candidate.parts[1:])
            ):
                raise ValueError(
                    "Transported cluster paths must be normalized absolute "
                    "POSIX paths."
                )
        else:
            encoded = _normalized_absolute_path(value).as_posix()
        return {
            "tag": "path",
            "value": encoded,
        }
    if value_type not in {list, tuple, dict}:
        raise TypeError(f"Unsupported submitted constant type: {value_type.__name__}.")

    identity = id(value)
    if identity in state.active:
        raise ValueError("Constants must not contain reference cycles.")
    state.active.add(identity)
    try:
        if value_type in {list, tuple}:
            return {
                "tag": "list" if value_type is list else "tuple",
                "value": [
                    _encode_constant(
                        item,
                        state=state,
                        depth=depth + 1,
                        preserve_paths=preserve_paths,
                    )
                    for item in value
                ],
            }
        entries = []
        for key, item in value.items():
            entries.append(
                {
                    "key": _encode_constant(
                        key,
                        state=state,
                        depth=depth + 1,
                        preserve_paths=preserve_paths,
                    ),
                    "value": _encode_constant(
                        item,
                        state=state,
                        depth=depth + 1,
                        preserve_paths=preserve_paths,
                    ),
                }
            )
        return {"tag": "dict", "value": entries}
    finally:
        state.active.remove(identity)


def encode_typed_constant(value: Any) -> dict[str, Any]:
    """Encode a supported constant without pickle or lossy fallbacks."""
    return _encode_constant(
        value,
        state=_CodecState(active=set()),
        depth=0,
        preserve_paths=False,
    )


def encode_cluster_typed_constant(value: Any) -> dict[str, Any]:
    """Encode constants while preserving explicit cluster Path spellings."""
    return _encode_constant(
        value,
        state=_CodecState(active=set()),
        depth=0,
        preserve_paths=True,
    )


def _plain_object(
    value: Any,
    *,
    keys: set[str],
    label: str,
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ValueError(f"{label} must contain exactly {sorted(keys)}.")
    return value


def _decode_constant(
    envelope: Any,
    *,
    state: _CodecState,
    depth: int,
    preserve_paths: bool,
) -> Any:
    state.visit(depth=depth)
    value = _plain_object(
        envelope,
        keys={"tag", "value"},
        label="Typed constant",
    )
    tag = value["tag"]
    payload = value["value"]
    if type(tag) is not str or tag not in _CONSTANT_TAGS:
        raise ValueError(f"Unknown typed constant tag: {tag!r}.")
    if tag == "none":
        if payload is not None:
            raise ValueError("none constant payload must be null.")
        return None
    if tag == "bool":
        if type(payload) is not bool:
            raise ValueError("bool constant payload has the wrong type.")
        return payload
    if tag == "int":
        if type(payload) is not int:
            raise ValueError("int constant payload has the wrong type.")
        return payload
    if tag == "float":
        if type(payload) is not float or not math.isfinite(payload):
            raise ValueError("float constant payload must be finite.")
        return payload
    if tag == "str":
        if type(payload) is not str:
            raise ValueError("str constant payload has the wrong type.")
        return payload
    if tag == "path":
        if type(payload) is not str or not payload:
            raise ValueError("path constant payload must be a non-empty string.")
        if preserve_paths:
            pure = PurePosixPath(payload)
            valid = (
                pure.is_absolute()
                and not payload.startswith("//")
                and str(pure) == payload
                and all(part not in {"", ".", ".."} for part in pure.parts[1:])
            )
        else:
            path = Path(payload)
            valid = (
                path.is_absolute()
                and _normalized_absolute_path(path).as_posix() == payload
            )
        if not valid:
            raise ValueError(
                "path constant payload must be a normalized absolute path."
            )
        return Path(payload)
    if type(payload) is not list:
        raise ValueError(f"{tag} constant payload must be a list.")
    if tag in {"list", "tuple"}:
        decoded = [
            _decode_constant(
                item,
                state=state,
                depth=depth + 1,
                preserve_paths=preserve_paths,
            )
            for item in payload
        ]
        return decoded if tag == "list" else tuple(decoded)

    result: dict[Any, Any] = {}
    for item in payload:
        entry = _plain_object(
            item,
            keys={"key", "value"},
            label="Typed dictionary entry",
        )
        key = _decode_constant(
            entry["key"],
            state=state,
            depth=depth + 1,
            preserve_paths=preserve_paths,
        )
        try:
            hash(key)
        except TypeError as exc:
            raise ValueError("Typed dictionary keys must be hashable.") from exc
        if key in result:
            raise ValueError("Typed dictionary keys must be unique.")
        result[key] = _decode_constant(
            entry["value"],
            state=state,
            depth=depth + 1,
            preserve_paths=preserve_paths,
        )
    return result


def decode_typed_constant(envelope: Any) -> Any:
    """Decode a strict typed constant envelope."""
    return _decode_constant(
        envelope,
        state=_CodecState(active=set()),
        depth=0,
        preserve_paths=False,
    )


def decode_cluster_typed_constant(envelope: Any) -> Any:
    """Decode transported cluster Paths without local path resolution."""
    return _decode_constant(
        envelope,
        state=_CodecState(active=set()),
        depth=0,
        preserve_paths=True,
    )


def _validate_input_mapping(inputs: Mapping[str, Any] | None) -> dict[str, Any]:
    if inputs is None:
        return {}
    if not isinstance(inputs, Mapping):
        raise TypeError("inputs must be a mapping or None.")
    result: dict[str, Any] = {}
    for name, value in inputs.items():
        if type(name) is not str or not name:
            raise TypeError("Workflow input names must be non-empty strings.")
        result[name] = value
    return result


def _validate_interface_address(
    port_id: Any,
    name: Any,
    *,
    label: str,
) -> None:
    if (
        type(port_id) is not str
        or not port_id
        or type(name) is not str
        or not name
    ):
        raise ValueError(f"{label} IDs and names must be non-empty strings.")


def _validate_target_names(
    workflow: "Workflow",
    targets: Sequence[str],
) -> tuple[str, ...]:
    if isinstance(targets, (str, bytes)) or not isinstance(targets, Sequence):
        raise TypeError("targets must be a sequence of structural node names.")
    names: list[str] = []
    seen: set[str] = set()
    for target in targets:
        if type(target) is not str or not target or "/" in target:
            raise ValueError(
                "Targets must be registered immediate structural node names."
            )
        if target not in workflow._nodes:
            raise ValueError(f"Unknown immediate workflow target {target!r}.")
        if target in seen:
            raise ValueError(f"Duplicate workflow target {target!r}.")
        seen.add(target)
        names.append(target)
    if not names:
        raise ValueError("Ad hoc target invocation requires at least one target.")
    return tuple(names)


def _input_transport_path(port_id: str) -> str:
    token = hashlib.sha256(port_id.encode("utf-8")).hexdigest()
    return f"inputs/{token}.parquet"


def _ensure_control_candidate(control_candidate: Path) -> Path:
    candidate = Path(control_candidate)
    if candidate.is_symlink():
        raise ValueError("Launcher control candidate must not be a symlink.")
    candidate.mkdir(parents=True, exist_ok=True)
    inputs_dir = candidate / "inputs"
    if inputs_dir.is_symlink():
        raise ValueError("Launcher inputs directory must not be a symlink.")
    inputs_dir.mkdir(exist_ok=True)
    return candidate


def serialize_invocation(
    workflow: "Workflow",
    *,
    inputs: Mapping[str, Any] | None = None,
    targets: Sequence[str] | None = None,
    control_candidate: Path,
    preserve_cluster_paths: bool = False,
) -> dict[str, Any]:
    """Serialize exactly one root-interface or ad-hoc invocation."""
    if targets is not None and inputs is not None:
        raise ValueError("inputs and targets are mutually exclusive.")
    if targets is not None:
        names = _validate_target_names(workflow, targets)
        return {
            "schema": INVOCATION_SCHEMA,
            "targets": list(names),
            "variant": "targets",
        }

    supplied = _validate_input_mapping(inputs)
    ports_by_name = {}
    for port in workflow._interface_inputs.values():
        _validate_interface_address(
            port.id,
            port.name,
            label="Workflow input",
        )
        ports_by_name[port.name] = port
    for port in workflow._interface_outputs.values():
        _validate_interface_address(
            port.id,
            port.name,
            label="Workflow output",
        )
    unknown = set(supplied) - set(ports_by_name)
    if unknown:
        raise ValueError(f"Unknown workflow input(s): {sorted(unknown)}.")

    planned: list[tuple[Any, Any, dict[str, Any] | None]] = []
    for port in workflow._interface_inputs.values():
        if port.name not in supplied:
            continue
        value = supplied[port.name]
        if port.kind == "dataframe":
            if not isinstance(value, pd.DataFrame):
                raise TypeError(
                    f"DataFrame workflow input {port.name!r} requires a DataFrame."
                )
            planned.append((port, value, None))
            continue
        if isinstance(value, pd.DataFrame):
            raise TypeError(
                f"Field workflow input {port.name!r} cannot contain a DataFrame."
            )
        planned.append(
            (
                port,
                value,
                (
                    encode_cluster_typed_constant(value)
                    if preserve_cluster_paths
                    else encode_typed_constant(value)
                ),
            )
        )

    candidate = _ensure_control_candidate(control_candidate)
    serialized_inputs: list[dict[str, Any]] = []
    for port, value, encoded in planned:
        if port.kind == "field":
            serialized_inputs.append(
                {
                    "id": port.id,
                    "kind": "field",
                    "name": port.name,
                    "value": encoded,
                }
            )
            continue
        relative_path = _input_transport_path(port.id)
        metadata = write_dataframe_transport(
            cast(pd.DataFrame, value),
            candidate / Path(relative_path),
            preserve_paths=preserve_cluster_paths,
        )
        serialized_inputs.append(
            {
                "dataframe": {"path": relative_path, **metadata},
                "id": port.id,
                "kind": "dataframe",
                "name": port.name,
            }
        )

    return {
        "inputs": serialized_inputs,
        "outputs": [
            {"id": port.id, "name": port.name}
            for port in workflow._interface_outputs.values()
        ],
        "schema": INVOCATION_SCHEMA,
        "variant": "root",
    }


def _protocol_error(
    message: str,
    error: BaseException | None = None,
) -> NoReturn:
    if error is None:
        raise LauncherProtocolError(message)
    raise LauncherProtocolError(message) from error


def _confined_input_path(control_dir: Path, value: Any) -> Path:
    if type(value) is not str:
        _protocol_error("DataFrame input path must be a string.")
    try:
        relative = validate_relative_posix_path(value)
    except ValueError as exc:
        _protocol_error("DataFrame input path is unsafe.", exc)
    parts = relative.split("/")
    if len(parts) != 2 or parts[0] != "inputs" or not parts[1].endswith(".parquet"):
        _protocol_error("DataFrame input path must name one inputs/ Parquet file.")

    root = Path(control_dir)
    if root.is_symlink() or not root.is_dir():
        _protocol_error("Launcher control directory is missing or unsafe.")
    root = root.resolve(strict=True)
    candidate = root
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink():
            _protocol_error("DataFrame input path contains a symlink.")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, OSError, ValueError) as exc:
        _protocol_error("DataFrame input path escapes its control directory.", exc)
    if not resolved.is_file():
        _protocol_error("DataFrame input path is not a regular file.")
    return resolved


def _load_root_invocation(
    workflow: "Workflow",
    payload: dict[str, Any],
    *,
    control_dir: Path,
) -> LoadedInvocation:
    if set(payload) != {"inputs", "outputs", "schema", "variant"}:
        _protocol_error("Root invocation contains missing or unknown fields.")
    raw_inputs = payload["inputs"]
    raw_outputs = payload["outputs"]
    if type(raw_inputs) is not list or type(raw_outputs) is not list:
        _protocol_error("Root invocation inputs and outputs must be lists.")

    ports_by_id = dict(workflow._interface_inputs)
    seen: set[str] = set()
    loaded: dict[str, Any] = {}
    for item in raw_inputs:
        if type(item) is not dict:
            _protocol_error("Root invocation input entries must be objects.")
        kind = item.get("kind")
        expected_keys = (
            {"id", "kind", "name", "value"}
            if kind == "field"
            else {"dataframe", "id", "kind", "name"}
        )
        if set(item) != expected_keys or kind not in {"field", "dataframe"}:
            _protocol_error("Root invocation input entry has an invalid shape.")
        port_id = item["id"]
        name = item["name"]
        if type(port_id) is not str or port_id in seen:
            _protocol_error("Root invocation input IDs must be unique strings.")
        port = ports_by_id.get(port_id)
        if (
            port is None
            or type(name) is not str
            or name != port.name
            or kind != port.kind
        ):
            _protocol_error(
                "Root invocation input does not match the current workflow interface."
            )
        seen.add(port_id)
        if kind == "field":
            try:
                loaded[name] = decode_typed_constant(item["value"])
            except (TypeError, ValueError) as exc:
                _protocol_error(
                    f"Root constant input {name!r} is invalid.",
                    exc,
                )
            continue

        dataframe = item["dataframe"]
        if type(dataframe) is not dict or set(dataframe) != _DATAFRAME_METADATA_KEYS:
            _protocol_error("Root DataFrame input metadata has an invalid shape.")
        path = _confined_input_path(control_dir, dataframe["path"])
        metadata = {key: value for key, value in dataframe.items() if key != "path"}
        try:
            loaded[name] = read_dataframe_transport(path, metadata)
        except (TypeError, ValueError, OSError) as exc:
            _protocol_error(
                f"Root DataFrame input {name!r} failed verification.",
                exc,
            )

    expected_outputs = [
        {"id": port.id, "name": port.name}
        for port in workflow._interface_outputs.values()
    ]
    if raw_outputs != expected_outputs:
        _protocol_error(
            "Root invocation outputs do not match the current workflow interface."
        )
    outputs = tuple(
        InvocationOutput(port_id=entry["id"], name=entry["name"])
        for entry in expected_outputs
    )
    return LoadedInvocation(
        variant="root",
        inputs=MappingProxyType(loaded),
        targets=(),
        outputs=outputs,
    )


def _load_target_invocation(
    workflow: "Workflow",
    payload: dict[str, Any],
) -> LoadedInvocation:
    if set(payload) != {"schema", "targets", "variant"}:
        _protocol_error("Ad hoc invocation contains missing or unknown fields.")
    raw_targets = payload["targets"]
    if type(raw_targets) is not list:
        _protocol_error("Ad hoc invocation targets must be a list.")
    try:
        targets = _validate_target_names(workflow, raw_targets)
    except (TypeError, ValueError) as exc:
        _protocol_error("Ad hoc invocation targets are invalid.", exc)
    return LoadedInvocation(
        variant="targets",
        inputs=MappingProxyType({}),
        targets=targets,
        outputs=(),
    )


def load_invocation(
    workflow: "Workflow",
    payload: Any,
    *,
    control_dir: Path,
) -> LoadedInvocation:
    """Validate and load an invocation before any DFK is acquired."""
    if type(payload) is not dict:
        _protocol_error("Launcher invocation must be an object.")
    if payload.get("schema") != INVOCATION_SCHEMA:
        _protocol_error("Launcher invocation schema is unsupported.")
    variant = payload.get("variant")
    if variant == "root":
        return _load_root_invocation(
            workflow,
            payload,
            control_dir=control_dir,
        )
    if variant == "targets":
        return _load_target_invocation(workflow, payload)
    _protocol_error("Launcher invocation variant is unsupported.")
