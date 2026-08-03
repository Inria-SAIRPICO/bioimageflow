"""Public discovery and strict invocation-time overrides for node path inputs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import UnionType
from typing import Annotated, Any, ClassVar, Literal, Union, cast, get_args, get_origin

from bioimageflow_core import extract_gui_meta
from pydantic import TypeAdapter, ValidationError as PydanticValidationError

from .types import LocalUpload


PathShape = Literal["path", "list", "tuple"]


def _strip_annotated(annotation: Any) -> Any:
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _nullable(annotation: Any) -> bool:
    annotation = _strip_annotated(annotation)
    origin = get_origin(annotation)
    return origin in {Union, UnionType} and type(None) in get_args(annotation)


def _path_shape(annotation: Any) -> PathShape | None:
    annotation = _strip_annotated(annotation)
    if annotation is Path:
        return "path"
    origin = get_origin(annotation)
    if origin in {Union, UnionType}:
        shapes = {
            shape
            for item in get_args(annotation)
            if item is not type(None) and (shape := _path_shape(item)) is not None
        }
        return cast(PathShape, next(iter(shapes))) if len(shapes) == 1 else None
    if origin is list:
        args = get_args(annotation)
        return "list" if len(args) == 1 and _path_shape(args[0]) is not None else None
    if origin is tuple:
        args = tuple(item for item in get_args(annotation) if item is not Ellipsis)
        return "tuple" if args and all(_path_shape(item) is not None for item in args) else None
    return None


def _collect_nodes(workflow: Any) -> dict[str, Any]:
    from bioimageflow.workflow_node import WorkflowNode

    result: dict[str, Any] = {}

    def collect(definition: Any, prefix: str = "") -> None:
        for name, node in definition._nodes.items():
            scoped = f"{prefix}/{name}" if prefix else name
            result[scoped] = node
            if isinstance(node, WorkflowNode):
                collect(node.workflow, scoped)

    collect(workflow)
    return result


def _current_paths(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, Path)):
        return (str(value),)
    if isinstance(value, (list, tuple)):
        return tuple(path for item in value for path in _current_paths(item))
    return ()


def _normalized_cluster_path(value: Path) -> str:
    encoded = value.as_posix()
    path = PurePosixPath(encoded)
    if (
        not path.is_absolute()
        or encoded.startswith("//")
        or str(path) != encoded
        or any(character in encoded for character in ("\x00", "\n", "\r"))
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise ValueError("Cluster paths must be normalized absolute POSIX paths.")
    return encoded


def _cluster_compatible(paths: tuple[str, ...]) -> bool:
    try:
        for value in paths:
            _normalized_cluster_path(Path(value))
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class RemoteNodePathInput:
    """One constant node input whose path value can be rebound for a remote run."""

    scoped_node_path: str
    input_name: str
    value_shape: PathShape
    nullable: bool
    path_picker: str | None
    current_paths: tuple[str, ...]
    cluster_compatible: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "scoped_node_path": self.scoped_node_path,
            "input_name": self.input_name,
            "value_shape": self.value_shape,
            "nullable": self.nullable,
            "path_picker": self.path_picker,
            "current_paths": list(self.current_paths),
            "cluster_compatible": self.cluster_compatible,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "RemoteNodePathInput":
        fields = {
            "scoped_node_path",
            "input_name",
            "value_shape",
            "nullable",
            "path_picker",
            "current_paths",
            "cluster_compatible",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("Invalid RemoteNodePathInput payload.")
        scoped_node_path = value["scoped_node_path"]
        input_name = value["input_name"]
        value_shape = value["value_shape"]
        nullable = value["nullable"]
        path_picker = value["path_picker"]
        current_paths = value["current_paths"]
        cluster_compatible = value["cluster_compatible"]
        if (
            type(scoped_node_path) is not str
            or not scoped_node_path
            or type(input_name) is not str
            or not input_name
            or value_shape not in {"path", "list", "tuple"}
            or type(nullable) is not bool
            or (path_picker is not None and type(path_picker) is not str)
            or type(current_paths) is not list
            or any(type(path) is not str for path in current_paths)
            or type(cluster_compatible) is not bool
        ):
            raise ValueError("Invalid RemoteNodePathInput payload.")
        return cls(
            scoped_node_path=scoped_node_path,
            input_name=input_name,
            value_shape=value_shape,
            nullable=nullable,
            path_picker=path_picker,
            current_paths=tuple(current_paths),
            cluster_compatible=cluster_compatible,
        )


@dataclass(frozen=True, slots=True)
class RemoteNodePathPlan:
    """Serializable non-reading discovery report for remote node path inputs."""

    SCHEMA: ClassVar[str] = "bioimageflow.remote_node_path_plan.v1"
    inputs: tuple[RemoteNodePathInput, ...]
    allocates_resources: bool = False
    reads_local_files: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "allocates_resources": self.allocates_resources,
            "reads_local_files": self.reads_local_files,
            "inputs": [item.to_dict() for item in self.inputs],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "RemoteNodePathPlan":
        if (
            not isinstance(value, dict)
            or set(value)
            != {"schema", "allocates_resources", "reads_local_files", "inputs"}
            or value["schema"] != cls.SCHEMA
            or value["allocates_resources"] is not False
            or value["reads_local_files"] is not False
            or not isinstance(value["inputs"], list)
        ):
            raise ValueError("Invalid RemoteNodePathPlan payload.")
        return cls(
            inputs=tuple(RemoteNodePathInput.from_dict(item) for item in value["inputs"])
        )


def inspect_remote_node_paths(workflow: Any) -> RemoteNodePathPlan:
    """Discover overridable path constants without reading files or allocating."""
    from bioimageflow.workflow_node import WorkflowNode

    records: list[RemoteNodePathInput] = []
    for scoped, node in _collect_nodes(workflow).items():
        if isinstance(node, WorkflowNode):
            continue
        annotations = node.tool.Inputs._get_all_annotations()
        for name, annotation in annotations.items():
            shape = _path_shape(annotation)
            if shape is None:
                continue
            if name in node._column_bindings or name in node._workflow_input_bindings:
                continue
            value = node._constant_bindings.get(
                name,
                getattr(node.tool.Inputs, name, None),
            )
            paths = _current_paths(value)
            metadata = extract_gui_meta(annotation)
            picker = getattr(getattr(metadata, "path_picker", None), "value", None)
            records.append(
                RemoteNodePathInput(
                    scoped_node_path=scoped,
                    input_name=name,
                    value_shape=shape,
                    nullable=_nullable(annotation),
                    path_picker=picker,
                    current_paths=paths,
                    cluster_compatible=_cluster_compatible(paths),
                )
            )
    return RemoteNodePathPlan(
        inputs=tuple(sorted(records, key=lambda item: (item.scoped_node_path, item.input_name)))
    )


def _validate_remote_value(value: Any) -> None:
    if value is None or isinstance(value, LocalUpload):
        return
    if isinstance(value, Path):
        _normalized_cluster_path(value)
        return
    if type(value) in {list, tuple}:
        for item in value:
            _validate_remote_value(item)
        return
    raise TypeError(
        "Remote node path overrides contain only Path, LocalUpload, None, list, or tuple values."
    )


def contains_local_upload(value: Any) -> bool:
    """Return whether a supported nested value explicitly requests an upload."""
    if isinstance(value, LocalUpload):
        return True
    if type(value) in {list, tuple}:
        return any(contains_local_upload(item) for item in value)
    return False


def _replace_uploads(value: Any) -> Any:
    if isinstance(value, LocalUpload):
        return value.path
    if type(value) is list:
        return [_replace_uploads(item) for item in value]
    if type(value) is tuple:
        return tuple(_replace_uploads(item) for item in value)
    return value


def validate_remote_path_value(annotation: Any, value: Any) -> None:
    """Validate one explicit remote path value against a declared annotation."""
    if _path_shape(annotation) is None:
        raise TypeError("The selected input is not path-shaped.")
    _validate_remote_value(value)
    try:
        TypeAdapter(annotation).validate_python(_replace_uploads(value))
    except PydanticValidationError as error:
        raise ValueError("The remote path value is invalid for its input.") from error


def normalize_node_input_overrides(
    workflow: Any,
    overrides: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[tuple[str, str, Any], ...]:
    """Validate and deterministically order live invocation-only overrides."""
    if overrides is None:
        return ()
    if not isinstance(overrides, Mapping):
        raise TypeError("node_input_overrides must be a mapping or None.")
    nodes = _collect_nodes(workflow)
    from bioimageflow.workflow_node import WorkflowNode

    result: list[tuple[str, str, Any]] = []
    for scoped in sorted(overrides):
        if type(scoped) is not str or not scoped:
            raise TypeError("Node override paths must be non-empty strings.")
        node = nodes.get(scoped)
        if node is None or isinstance(node, WorkflowNode):
            raise ValueError(f"Unknown or non-tool scoped node path: {scoped!r}.")
        fields = overrides[scoped]
        if not isinstance(fields, Mapping):
            raise TypeError(f"Overrides for {scoped!r} must be a mapping.")
        annotations = node.tool.Inputs._get_all_annotations()
        for name in sorted(fields):
            if type(name) is not str or name not in annotations:
                raise ValueError(f"Unknown input {name!r} on node {scoped!r}.")
            if _path_shape(annotations[name]) is None:
                raise TypeError(f"Node input {scoped!r}/{name!r} is not path-shaped.")
            if name in node._column_bindings or name in node._workflow_input_bindings:
                raise ValueError(f"Connected node input {scoped!r}/{name!r} cannot be overridden.")
            value = fields[name]
            try:
                validate_remote_path_value(annotations[name], value)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"Override for node input {scoped!r}/{name!r} is invalid."
                ) from error
            result.append((scoped, name, value))
    return tuple(result)


def apply_node_input_overrides(
    workflow: Any,
    overrides: tuple[tuple[str, str, Any], ...],
) -> None:
    """Apply already decoded values to a fresh cluster-side workflow graph."""
    normalized = normalize_node_input_overrides(
        workflow,
        {
            scoped: {
                name: value
                for candidate_scoped, name, value in overrides
                if candidate_scoped == scoped
            }
            for scoped in dict.fromkeys(item[0] for item in overrides)
        },
    )
    nodes = _collect_nodes(workflow)
    for scoped, name, value in normalized:
        node = nodes[scoped]
        node._constant_bindings[name] = value
        node._kwargs[name] = value


__all__ = [
    "RemoteNodePathInput",
    "RemoteNodePathPlan",
    "inspect_remote_node_paths",
]
