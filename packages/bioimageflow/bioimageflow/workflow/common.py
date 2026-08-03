"""Shared workflow values and helpers."""

# Focused workflow modules import the shared values they need.
# ruff: noqa: F401

import importlib
import importlib.util
import base64
import hashlib
import inspect
import json
import logging
import sys
import tempfile
import threading
import zipfile
import copy
import uuid
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast, overload
from typing import TYPE_CHECKING

from bioimageflow.node import (
    ColumnRef,
    set_active_workflow,
    get_active_workflow,
    _reset_name_counters,
    _error_capture,
    Node,
)
from bioimageflow.events import ProgressEvent
from bioimageflow.validation import (
    ValidationError,
    serialize_constant,
    deserialize_constant,
)

if TYPE_CHECKING:
    from bioimageflow.engine import (
        DefaultEngine,
        ResourceLifetime,
        NodeStep,
        NodePlan,
    )
    from bioimageflow.env_manager import WetlandsEnvManager
    from .model import Workflow

from bioimageflow_core.environment import EnvironmentSpec
from bioimageflow_core.tool import ProcessingTool


logger = logging.getLogger("bioimageflow")


class _Missing:
    def __deepcopy__(self, memo: dict[int, Any]) -> "_Missing":
        return self


MISSING = _Missing()


@dataclass(frozen=True)
class WorkflowInputRef:
    """Symbolic reference to a public input owned by one workflow."""

    workflow: "Workflow"
    port_id: str
    name: str
    kind: Literal["field", "dataframe"]
    annotation: Any = None

    def __deepcopy__(self, memo: dict[int, Any]) -> "WorkflowInputRef":
        # Ownership is definition identity, not mutable payload. Keeping the
        # reference also avoids recursively copying the complete Workflow from
        # a Node's construction-time bookkeeping.
        return self


@dataclass
class WorkflowInputPort:
    """Canonical definition of one workflow input port."""

    id: str
    name: str
    kind: Literal["field", "dataframe"]
    annotation: Any = None
    schema: dict[str, Any] | None = None
    default: Any = MISSING
    targets: list[dict[str, Any]] = dataclass_field(default_factory=list)

    def has_fallback(self, workflow: "Workflow") -> bool:
        if self.default is not MISSING:
            return True
        for target in self.targets:
            node = workflow._nodes.get(target["node"])
            if node is None:
                continue
            endpoint = target["port"]
            if (
                endpoint["kind"] == "field"
                and endpoint["name"] in node._constant_bindings
            ):
                return True
            if endpoint["kind"] == "field" and hasattr(
                node.tool.Inputs, endpoint["name"]
            ):
                return True
            if endpoint["kind"] == "workflow":
                from bioimageflow.workflow_node import WorkflowNode

                if isinstance(node, WorkflowNode):
                    child_port = node.workflow._interface_inputs.get(endpoint["id"])
                    if child_port is not None and child_port.has_fallback(
                        node.workflow
                    ):
                        return True
        return False


@dataclass
class WorkflowOutputPort:
    """Canonical definition of one workflow output port."""

    id: str
    name: str
    annotation: Any
    schema: dict[str, Any] | None
    source_node: str
    source_output: str


def _new_port_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _annotation_schema(annotation: Any) -> dict[str, Any] | None:
    if annotation is None:
        return None
    from bioimageflow.validation import (
        _display_type_name,
        extract_image_spec,
        serialize_image_spec,
    )

    return {
        "type": _display_type_name(annotation),
        "image_spec": serialize_image_spec(extract_image_spec(annotation)),
    }


def _absolute_runtime_path(path: str | Path) -> Path:
    """Return an absolute path without requiring the target to exist."""
    expanded = Path(path).expanduser()
    if expanded.is_absolute():
        return expanded
    return Path.cwd() / expanded


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


@dataclass(frozen=True)
class _CustomToolBundle:
    """Loaded embedded custom-tool source bundle."""

    source_id: str
    source_hash: str
    module: Any | None = None
    scoped_root: str | None = None
    root_package: str | None = None
    sys_path: str | None = None


@dataclass
class WorkflowEnvironment:
    """Mutable launch configuration for a Wetlands environment."""

    name: str
    spec: EnvironmentSpec | None = None
    max_workers: int = 0
    worker_timeout: float | None = None


@dataclass(frozen=True)
class OutputView:
    """Human-facing output materialization policy."""

    mode: Literal["none", "pointer", "symlink", "copy", "hardlink"] = "none"
    scope: Literal["latest", "runs", "both"] = "latest"

    def __post_init__(self) -> None:
        if self.mode not in {"none", "pointer", "symlink", "copy", "hardlink"}:
            raise ValueError(
                "Invalid output_view mode. Expected 'none', 'pointer', 'symlink', 'copy', or 'hardlink'."
            )
        if self.scope not in {"latest", "runs", "both"}:
            raise ValueError(
                "Invalid output_view scope. Expected 'latest', 'runs', or 'both'."
            )

    def to_dict(self) -> dict[str, str]:
        return {"mode": self.mode, "scope": self.scope}


def _coerce_output_view_mode(
    value: str,
) -> Literal["none", "pointer", "symlink", "copy", "hardlink"]:
    if value not in {"none", "pointer", "symlink", "copy", "hardlink"}:
        raise ValueError(
            "Invalid output_view mode. Expected 'none', 'pointer', 'symlink', 'copy', or 'hardlink'."
        )
    return cast(Literal["none", "pointer", "symlink", "copy", "hardlink"], value)


def _coerce_output_view_scope(value: str) -> Literal["latest", "runs", "both"]:
    if value not in {"latest", "runs", "both"}:
        raise ValueError(
            "Invalid output_view scope. Expected 'latest', 'runs', or 'both'."
        )
    return cast(Literal["latest", "runs", "both"], value)


def _normalize_output_view(
    value: "OutputView | Mapping[str, Any] | str | None",
) -> OutputView | None:
    if value is None:
        return None
    if isinstance(value, OutputView):
        return value
    if isinstance(value, str):
        return OutputView(mode=_coerce_output_view_mode(value))
    if isinstance(value, Mapping):
        return OutputView(
            mode=_coerce_output_view_mode(str(value.get("mode", "none"))),
            scope=_coerce_output_view_scope(str(value.get("scope", "latest"))),
        )
    raise TypeError(
        "output_view must be None, a mode string, a mapping, or OutputView."
    )


@dataclass(frozen=True)
class InvalidatedSelection:
    """A cache selection removed by :meth:`Workflow.invalidate`."""

    node_name: str
    result_key: str
    selected_record_id: str | None
    status: Literal["removed", "corrupt_removed"] = "removed"


def _remove_current_selection(
    storage: "Storage",
    result_key: str,
    *,
    node_name: str,
) -> InvalidatedSelection | None:
    selection = _inspect_current_selection(
        storage,
        result_key,
        node_name=node_name,
    )
    if selection is None:
        return None
    current_path = storage.result_dir(result_key) / "current.json"
    current_path.unlink()
    return selection


def _inspect_current_selection(
    storage: "Storage",
    result_key: str,
    *,
    node_name: str,
) -> InvalidatedSelection | None:
    """Read one current cache selection without changing it."""
    from bioimageflow.storage import CacheCorruptionError

    current_path = storage.result_dir(result_key) / "current.json"
    if not current_path.exists():
        return None
    selected_record_id: str | None = None
    status: Literal["removed", "corrupt_removed"] = "removed"
    try:
        selected_record_id = storage.load_current(result_key).record_id  # type: ignore[union-attr]
    except CacheCorruptionError:
        status = "corrupt_removed"
        try:
            raw = json.loads(current_path.read_text())
            if isinstance(raw, dict) and isinstance(raw.get("record_id"), str):
                selected_record_id = raw["record_id"]
        except (OSError, json.JSONDecodeError):
            selected_record_id = None
    return InvalidatedSelection(
        node_name=node_name,
        result_key=result_key,
        selected_record_id=selected_record_id,
        status=status,
    )


def _clear_currents_for_node(
    storage_path: str | Path,
    node_name: str,
    *,
    kind: Literal["dataframe_tool", "processing_tool"],
) -> set[InvalidatedSelection]:
    from bioimageflow.cache import (
        iter_dataframe_result_metadata,
        iter_processing_result_metadata,
    )
    from bioimageflow.storage import Storage

    results_root = Path(storage_path) / "cache" / "v1" / "results"
    if not results_root.exists():
        return set()
    invalidated: set[InvalidatedSelection] = set()
    storage = Storage(storage_path)
    metadata_iter = (
        iter_dataframe_result_metadata
        if kind == "dataframe_tool"
        else iter_processing_result_metadata
    )
    for metadata in metadata_iter(storage_path):
        if metadata.get("node") != node_name:
            continue
        result_key = metadata.get("result_key")
        if not isinstance(result_key, str):
            continue
        selection = _remove_current_selection(storage, result_key, node_name=node_name)
        if selection is not None:
            invalidated.add(selection)
    return invalidated


if TYPE_CHECKING:
    from bioimageflow.storage import Storage
