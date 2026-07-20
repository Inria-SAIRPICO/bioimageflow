"""Workflow container and progress events."""

import importlib
import importlib.util
import base64
import hashlib
import inspect
import json
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
from bioimageflow.validation import (
    ValidationError,
    serialize_constant,
    deserialize_constant,
)

if TYPE_CHECKING:
    from bioimageflow.engine import DefaultEngine, EnvironmentLifetime, NodeStep, NodePlan
    from bioimageflow.env_manager import WetlandsEnvManager

from bioimageflow_core.environment import EnvironmentSpec
from bioimageflow_core.tool import ProcessingTool


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
            if endpoint["kind"] == "field" and endpoint["name"] in node._constant_bindings:
                return True
            if endpoint["kind"] == "field" and hasattr(
                node.tool.Inputs, endpoint["name"]
            ):
                return True
            if endpoint["kind"] == "workflow":
                from bioimageflow.workflow_node import WorkflowNode

                if isinstance(node, WorkflowNode):
                    child_port = node.workflow._interface_inputs.get(endpoint["id"])
                    if child_port is not None and child_port.has_fallback(node.workflow):
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
    from bioimageflow.validation import _display_type_name, extract_image_spec, serialize_image_spec

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
    worker_env: Callable[[int], dict[str, str]] | None = None
    worker_timeout: float | None = None


@dataclass
class ProgressEvent:
    """Progress event reported by the engine."""
    node_name: str
    status: str  # "started", "row_progress", "row_complete", "completed", "cached", "failed", "cancelled"
    row: int = 0
    total_rows: int = 0
    message: str | None = None
    current: int | None = None
    maximum: int | None = None
    timestamp: float = 0.0
    result_key: str | None = None
    record_id: str | None = None


@dataclass(frozen=True)
class OutputView:
    """Human-facing output materialization policy."""

    mode: Literal["none", "symlink", "copy", "hardlink"] = "none"
    scope: Literal["latest", "runs", "both"] = "latest"

    def __post_init__(self) -> None:
        if self.mode not in {"none", "symlink", "copy", "hardlink"}:
            raise ValueError(
                "Invalid output_view mode. Expected 'none', 'symlink', 'copy', or 'hardlink'."
            )
        if self.scope not in {"latest", "runs", "both"}:
            raise ValueError(
                "Invalid output_view scope. Expected 'latest', 'runs', or 'both'."
            )

    def to_dict(self) -> dict[str, str]:
        return {"mode": self.mode, "scope": self.scope}


def _coerce_output_view_mode(value: str) -> Literal["none", "symlink", "copy", "hardlink"]:
    if value not in {"none", "symlink", "copy", "hardlink"}:
        raise ValueError(
            "Invalid output_view mode. Expected 'none', 'symlink', 'copy', or 'hardlink'."
        )
    return cast(Literal["none", "symlink", "copy", "hardlink"], value)


def _coerce_output_view_scope(value: str) -> Literal["latest", "runs", "both"]:
    if value not in {"latest", "runs", "both"}:
        raise ValueError(
            "Invalid output_view scope. Expected 'latest', 'runs', or 'both'."
        )
    return cast(Literal["latest", "runs", "both"], value)


def _normalize_output_view(value: "OutputView | Mapping[str, Any] | str | None") -> OutputView | None:
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
    raise TypeError("output_view must be None, a mode string, a mapping, or OutputView.")


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
    current_path.unlink()
    return InvalidatedSelection(
        node_name=node_name,
        result_key=result_key,
        selected_record_id=selected_record_id,
        status=status,
    )


def _clear_currents_for_node(
    storage_path: str | Path,
    node_name: str,
    known_sig_hashes: set[str],
    *,
    kind: Literal["dataframe_tool", "processing_tool"],
) -> set[InvalidatedSelection]:
    from bioimageflow.cache import iter_dataframe_result_metadata, iter_processing_result_metadata
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
    for metadata in metadata_iter(
        storage_path,
        {node_name: known_sig_hashes},
    ):
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


class Workflow:
    """Holds the DAG and provides configuration for execution."""

    MISSING = MISSING

    def __init__(
        self,
        storage_path: str | Path = "./bif_data",
        *,
        name: str = "workflow",
        display_name: str | None = None,
        engine: str = "wetlands",
        execution: str = "parallel",
        on_progress: Callable[[ProgressEvent], None] | None = None,
        wetlands_config: dict[str, Any] | None = None,
        max_workers: int = 1,
        output_view: OutputView | Mapping[str, Any] | str | None = None,
    ) -> None:
        if not name or "/" in name:
            raise ValueError("Workflow name must be non-empty and may not contain '/'.")
        if engine not in {"direct", "wetlands"}:
            raise ValueError(
                f"Unknown engine '{engine}'. Expected 'direct' or 'wetlands'."
            )
        if execution not in {"parallel", "sequential"}:
            raise ValueError(
                f"Unknown execution '{execution}'. Expected 'parallel' or 'sequential'."
            )
        self.name = name
        self.display_name = display_name if display_name is not None else name
        self._storage_path_config = str(storage_path)
        self.storage_path = _absolute_runtime_path(storage_path)
        self.engine_type = engine
        self.execution = execution
        self.on_progress = on_progress
        self.wetlands_config = wetlands_config
        self.max_workers = max_workers
        self.output_view = _normalize_output_view(output_view)
        self._env_configs: dict[str, WorkflowEnvironment] = {}
        self._cancel_event = threading.Event()
        self._nodes: dict[str, Node] = {}
        self._prev_workflow: Any = None
        self._dev_mode: bool = False
        # Build-time errors and failed-node bookkeeping. These are
        # populated by ``from_dict`` (in collecting modes) and exposed
        # via the public ``errors`` / ``failed_nodes`` / ``is_partial``
        # properties so external callers don't have to remember to
        # capture the second tuple element of ``from_dict``.
        self._build_errors: list[ValidationError] = []
        self._failed_nodes: dict[str, ValidationError] = {}
        self._expected_node_names: set[str] | None = None
        self._run_view_context: dict[str, Any] | None = None
        self._interface_inputs: dict[str, WorkflowInputPort] = {}
        self._interface_outputs: dict[str, WorkflowOutputPort] = {}
        self._captured_custom_sources: list[dict[str, Any]] | None = None
        self._accept_root_dataframes = False

    def input(
        self,
        name: str,
        annotation: Any = None,
        *,
        kind: Literal["field", "dataframe"] = "field",
        default: Any = MISSING,
        id: str | None = None,
    ) -> WorkflowInputRef:
        """Declare and return a symbolic public workflow input."""
        if name == "name":
            raise ValueError("'name' is reserved for a workflow invocation's node name.")
        if not name:
            raise ValueError("Workflow input names must be non-empty.")
        if kind not in {"field", "dataframe"}:
            raise ValueError("Workflow input kind must be 'field' or 'dataframe'.")
        if kind == "field" and annotation is None:
            raise ValueError("Field workflow inputs require an annotation.")
        if any(port.name == name for port in self._interface_inputs.values()) or any(
            port.name == name for port in self._interface_outputs.values()
        ):
            raise ValueError(f"Workflow interface name '{name}' is not unique.")
        port_id = id or _new_port_id("input")
        if port_id in self._interface_inputs or port_id in self._interface_outputs:
            raise ValueError(f"Workflow interface ID '{port_id}' is not unique.")
        port = WorkflowInputPort(
            id=port_id,
            name=name,
            kind=kind,
            annotation=annotation,
            schema=_annotation_schema(annotation) if kind == "field" else None,
            default=default,
        )
        self._interface_inputs[port_id] = port
        return WorkflowInputRef(self, port_id, name, kind, annotation)

    def _input_ref(self, port_id: str) -> WorkflowInputRef:
        port = self._interface_inputs[port_id]
        return WorkflowInputRef(self, port.id, port.name, port.kind, port.annotation)

    def expose_input(
        self,
        node: Node,
        target: str | int,
        *,
        name: str,
        annotation: Any = None,
        kind: Literal["field", "dataframe"] = "field",
        default: Any = MISSING,
        id: str | None = None,
    ) -> WorkflowInputRef:
        """Publish an existing node target through the canonical interface."""
        if node.name not in self._nodes or self._nodes[node.name] is not node:
            raise ValueError("The exposed target node must belong to this workflow.")
        if kind == "field" and annotation is None:
            annotations = node.tool.Inputs._get_all_annotations()
            annotation = annotations.get(str(target))
        ref = self.input(
            name,
            annotation,
            kind=kind,
            default=default,
            id=id,
        )
        self._bind_input_target(ref, node, target, kind=kind)
        if kind == "field":
            node._workflow_input_bindings[str(target)] = ref
            if str(target) in node._constant_bindings:
                node._workflow_input_fallback_constants.add(str(target))
            node._column_bindings.pop(str(target), None)
        else:
            index = int(target)
            node._workflow_dataframe_bindings[index] = ref
            while len(node._args) <= index:
                node._args.append(None)
            node._args[index] = None
        return ref

    def _input_by_name(self, name: str) -> WorkflowInputPort | None:
        return next((port for port in self._interface_inputs.values() if port.name == name), None)

    def _output_by_name(self, name: str) -> WorkflowOutputPort | None:
        return next((port for port in self._interface_outputs.values() if port.name == name), None)

    def output(self, name: str, source: Any, *, id: str | None = None) -> None:
        """Publish an internal node column as a workflow output."""
        from bioimageflow.node import ColumnRef
        from bioimageflow.workflow_node import WorkflowNode

        if not isinstance(source, ColumnRef):
            raise TypeError("Workflow.output source must be a ColumnRef.")
        if source.node.name not in self._nodes or self._nodes[source.node.name] is not source.node:
            raise ValueError("Workflow output sources must belong to the same workflow.")
        if any(port.name == name for port in self._interface_inputs.values()) or any(
            port.name == name for port in self._interface_outputs.values()
        ):
            raise ValueError(f"Workflow interface name '{name}' is not unique.")
        port_id = id or _new_port_id("output")
        if port_id in self._interface_inputs or port_id in self._interface_outputs:
            raise ValueError(f"Workflow interface ID '{port_id}' is not unique.")

        annotation: Any = Any
        schema: dict[str, Any] | None = None
        if isinstance(source.node, WorkflowNode):
            child_port = source.node.workflow._interface_outputs.get(source.column)
            if child_port is None:
                raise ValueError(f"Unknown child workflow output port '{source.column}'.")
            annotation, schema = child_port.annotation, copy.deepcopy(child_port.schema)
        else:
            outputs = getattr(source.node.tool, "Outputs", None)
            annotations = outputs._get_all_annotations() if outputs is not None else {}
            if source.column in annotations:
                annotation = annotations[source.column]
                schema = _annotation_schema(annotation)
            else:
                resolved = source.node.get_output_schema() or {}
                if source.column not in resolved:
                    raise ValueError(
                        f"Column '{source.column}' is not a resolved output of node '{source.node.name}'."
                    )
                schema = copy.deepcopy(resolved[source.column])
        self._interface_outputs[port_id] = WorkflowOutputPort(
            id=port_id,
            name=name,
            annotation=annotation,
            schema=schema,
            source_node=source.node.name,
            source_output=source.column,
        )

    def _bind_input_target(
        self,
        ref: WorkflowInputRef,
        node: Node,
        target: str | int,
        *,
        kind: Literal["field", "dataframe"],
    ) -> None:
        if ref.workflow is not self or get_active_workflow() is not self:
            raise ValueError("A symbolic workflow input may only be bound in its owning active workflow.")
        port = self._interface_inputs.get(ref.port_id)
        if port is None or port.kind != kind:
            raise ValueError(
                f"Workflow input '{ref.name}' cannot target a {kind} input."
            )
        from bioimageflow.workflow_node import WorkflowNode

        if isinstance(node, WorkflowNode):
            descriptor = {"kind": "workflow", "id": str(target)}
        elif kind == "field":
            if target in node._column_bindings:
                raise ValueError(
                    f"Node '{node.name}' input '{target}' already has an internal data edge."
                )
            descriptor = {"kind": "field", "name": str(target)}
        else:
            index = int(target)
            if index < len(node._args) and isinstance(node._args[index], Node):
                raise ValueError(
                    f"Node '{node.name}' positional input {index} already has an internal data edge."
                )
            descriptor = {"kind": "positional", "index": index}
        record = {"node": node.name, "port": descriptor}
        for other in self._interface_inputs.values():
            if other.id != port.id and record in other.targets:
                raise ValueError(
                    f"Internal target {node.name}:{target} is already published by '{other.name}'."
                )
        if record not in port.targets:
            port.targets.append(record)

    def _apply_interface_binding(self, port_id: str, value: Any) -> None:
        """Substitute one boundary value at every target in this definition."""
        from bioimageflow.node import ColumnRef, Node
        from bioimageflow.workflow_node import WorkflowNode

        port = self._interface_inputs[port_id]
        for target in port.targets:
            node = self._nodes[target["node"]]
            endpoint = target["port"]
            if isinstance(node, WorkflowNode):
                node.bind_port(endpoint["id"], value)
                continue
            if endpoint["kind"] == "field":
                field_name = endpoint["name"]
                if isinstance(value, ColumnRef):
                    node._constant_bindings.pop(field_name, None)
                    node._workflow_input_fallback_constants.discard(field_name)
                    node._column_bindings[field_name] = value
                    node._upstream_nodes.add(value.node)
                else:
                    node._column_bindings.pop(field_name, None)
                    node._constant_bindings[field_name] = value
                    node._workflow_input_fallback_constants.discard(field_name)
            else:
                import pandas as pd

                if not isinstance(value, (Node, pd.DataFrame)):
                    raise TypeError(
                        f"DataFrame workflow input '{port.name}' requires a complete DataFrame or upstream node."
                    )
                index = endpoint["index"]
                while len(node._args) <= index:
                    node._args.append(None)
                node._args[index] = value
                if isinstance(value, Node):
                    node._upstream_nodes.add(value)

    def _snapshot_definition(self, memo: dict[int, Any] | None = None) -> "Workflow":
        """Copy definition state without copying live execution state."""
        snapshot = Workflow(
            name=self.name,
            display_name=self.display_name,
            storage_path=self._storage_path_config,
            engine=self.engine_type,
            execution=self.execution,
            wetlands_config=copy.deepcopy(self.wetlands_config),
            max_workers=self.max_workers,
            output_view=copy.deepcopy(self.output_view),
        )
        memo = memo if memo is not None else {}
        memo[id(self)] = snapshot
        snapshot._nodes = copy.deepcopy(self._nodes, memo)
        snapshot._interface_inputs = copy.deepcopy(self._interface_inputs, memo)
        snapshot._interface_outputs = copy.deepcopy(self._interface_outputs, memo)
        snapshot._captured_custom_sources = copy.deepcopy(self._captured_custom_sources, memo)
        return snapshot

    def __call__(self, *, name: str | None = None, **bindings: Any) -> Any:
        """Capture this definition as a WorkflowNode in the active parent."""
        from bioimageflow.workflow_node import WorkflowNode

        by_name = {port.name: port for port in self._interface_inputs.values()}
        unknown = set(bindings) - set(by_name)
        if unknown:
            raise ValueError(
                f"Unknown workflow input(s) for '{self.name}': {sorted(unknown)}."
            )
        stable_bindings = {by_name[key].id: value for key, value in bindings.items()}
        return WorkflowNode(
            self._snapshot_definition(),
            name=name,
            bindings=stable_bindings,
        )

    def __enter__(self) -> "Workflow":
        self._prev_workflow = get_active_workflow()
        set_active_workflow(self)
        _reset_name_counters()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Literal[False]:
        set_active_workflow(self._prev_workflow)
        return False

    def topological_order(self) -> list[str]:
        """Return node names in dependency order. Raises on cycle.

        Thin wrapper over :func:`bioimageflow.engine.topological_order`.
        If the graph may contain a cycle, call :meth:`validate` first.
        """
        from bioimageflow.engine import topological_order as _topo
        return _topo(self)

    def invalidate(
        self,
        node_ids: "Iterable[str]",
        *,
        cascade: bool = True,
    ) -> set[InvalidatedSelection]:
        """Remove cache selections for the given nodes.

        Returns the selections whose ``current.json`` pointers were
        removed. ``cascade=True`` (the default) also removes selections
        for every node transitively downstream of each input node, so a
        subsequent run recomputes or reselects everything that depended on
        the changed node. Immutable ``records/<record-id>/`` directories
        are retained.

        ``KeyError`` is raised if any name in ``node_ids`` is not
        registered with this workflow — matching the existing behavior
        of :meth:`downstream_of`.

        Concurrency
        -----------
        This method is **not** safe to call concurrently with
        :meth:`compute` on the same workflow. The library does not
        currently expose a public lock primitive; callers that need
        to invalidate while a compute is in flight must coordinate
        externally (e.g., cancel + join + invalidate).
        """
        from bioimageflow.dataframe_tool import DataFrameTool
        from bioimageflow.engine import DefaultEngine
        from bioimageflow.storage import Storage
        from bioimageflow.workflow_node import WorkflowNode
        from bioimageflow_core.tool import ProcessingTool

        scoped_nodes: dict[str, Node] = {}

        def collect(definition: Workflow, prefix: str = "") -> None:
            for local_name, candidate in definition._nodes.items():
                path = f"{prefix}/{local_name}" if prefix else local_name
                scoped_nodes[path] = candidate
                if isinstance(candidate, WorkflowNode):
                    collect(candidate.workflow, path)

        collect(self)
        targets: set[str] = set()
        for nid in node_ids:
            if nid not in scoped_nodes:
                raise KeyError(
                    f"Node '{nid}' not found. Available: {list(scoped_nodes)}"
                )
            targets.add(nid)
            if isinstance(scoped_nodes[nid], WorkflowNode):
                targets.update(
                    path for path in scoped_nodes if path.startswith(f"{nid}/")
                )

        invalidated: set[InvalidatedSelection] = set()
        self._discover_graph(list(self._nodes.values()))
        engine = DefaultEngine(use_wetlands=False)
        try:
            plan = engine.plan(self)
        except Exception as exc:
            from bioimageflow.storage import CacheCorruptionError

            if not isinstance(exc, CacheCorruptionError):
                raise
            plan = {}
        if cascade:
            downstream: dict[str, set[str]] = {name: set() for name in plan}
            for name, entry in plan.items():
                for upstream in entry.upstream:
                    downstream.setdefault(upstream, set()).add(name)
            queue = list(targets)
            while queue:
                current = queue.pop()
                for dependent in downstream.get(current, ()):
                    if dependent not in targets:
                        targets.add(dependent)
                        queue.append(dependent)

        storage = Storage(self.storage_path)
        for name in targets:
            node = scoped_nodes[name]
            if isinstance(node, WorkflowNode):
                continue
            entry = plan.get(name)
            sig_hash = entry.logical_signature if entry is not None else None
            result_key = entry.final_result_key if entry is not None else None
            known_sig_hashes = {sig_hash} if sig_hash and result_key is not None else set()
            if isinstance(node.tool, DataFrameTool):
                if result_key is not None:
                    selection = _remove_current_selection(storage, result_key, node_name=name)
                    if selection is not None:
                        invalidated.add(selection)
                invalidated.update(
                    _clear_currents_for_node(
                        self.storage_path,
                        name,
                        known_sig_hashes,
                        kind="dataframe_tool",
                    )
                )
            if isinstance(node.tool, ProcessingTool):
                if result_key is not None:
                    selection = _remove_current_selection(storage, result_key, node_name=name)
                    if selection is not None:
                        invalidated.add(selection)
                invalidated.update(
                    _clear_currents_for_node(
                        self.storage_path,
                        name,
                        known_sig_hashes,
                        kind="processing_tool",
                    )
                )
        return invalidated

    def _dataframe_tool_signature_params(self, node: Node) -> dict[str, Any]:
        from bioimageflow.validation import is_path_type

        input_annotations = node.tool.Inputs._get_all_annotations()
        args_dict = dict(node._constant_bindings)
        for field_name in input_annotations:
            if field_name not in args_dict and hasattr(node.tool.Inputs, field_name):
                args_dict[field_name] = getattr(node.tool.Inputs, field_name)
            if field_name in args_dict and is_path_type(input_annotations[field_name]):
                args_dict[field_name] = _absolute_runtime_path(args_dict[field_name])
        return args_dict

    def downstream_of(self, node_name: str) -> set[str]:
        """Return node names transitively downstream of ``node_name``.

        Excludes ``node_name`` itself. Useful for callers (GUIs, external
        schedulers) that need to mark dependents as cache-invalidated
        after a parameter change.
        """
        if node_name not in self._nodes:
            raise KeyError(
                f"Node '{node_name}' not found. Available: {list(self._nodes)}"
            )

        # Build reverse adjacency (edge from upstream → downstream).
        reverse: dict[str, set[str]] = {n: set() for n in self._nodes}
        for n, node in self._nodes.items():
            for up in node._upstream_nodes:
                if up.name in reverse:
                    reverse[up.name].add(n)
            for arg in node._args:
                if isinstance(arg, Node) and arg.name in reverse:
                    reverse[arg.name].add(n)

        visited: set[str] = set()
        queue: list[str] = list(reverse[node_name])
        while queue:
            nxt = queue.pop()
            if nxt in visited:
                continue
            visited.add(nxt)
            queue.extend(reverse.get(nxt, ()))
        return visited

    def plan(self, *, dev_mode: bool = False) -> "dict[str, NodePlan]":
        """Return a per-node cache-status plan.

        Instantiates a non-Wetlands :class:`DefaultEngine` and calls its
        :meth:`plan`. No tool code runs.
        """
        from bioimageflow.engine import DefaultEngine
        self._dev_mode = dev_mode
        self._discover_graph(list(self._nodes.values()))
        return DefaultEngine(use_wetlands=False).plan(self)

    def create_engine(
        self,
        *,
        environment_lifetime: "EnvironmentLifetime | str" = "execution",
        env_manager: "WetlandsEnvManager | None" = None,
    ) -> "DefaultEngine":
        """Create an engine preserving this workflow's execution configuration.

        ``environment_lifetime`` controls whether Wetlands workers stop after
        each execution (``"execution"``), remain warm until ``engine.close()``
        (``"engine"``), or are owned entirely by the caller
        (``"external"``). An existing manager can be injected so multiple
        workflows and engines share the same worker environments.
        """
        from bioimageflow.engine import DefaultEngine, SequentialEngine

        use_wetlands = self.engine_type == "wetlands"
        kwargs: dict[str, Any] = {
            "use_wetlands": use_wetlands,
            "wetlands_config": self.wetlands_config,
            "environment_lifetime": environment_lifetime,
            "env_manager": env_manager,
        }
        if self.execution == "sequential":
            return SequentialEngine(**kwargs)
        return DefaultEngine(**kwargs)

    def _make_engine(self) -> "DefaultEngine":
        """Compatibility wrapper for the former private engine factory."""
        return self.create_engine()

    def validate(
        self,
        *,
        dev_mode: bool = False,
        _recursion_stack: tuple[int, ...] = (),
    ) -> list[ValidationError]:
        """Return all domain-level problems in this workflow.

        Runs, in order:

        1. Cycle detection (one error per cycle).
        2. Type compatibility on every column binding.
        3. Missing-required-input check for every node.
        4. Pydantic validation of every node's supplied constants.
        5. Recursive validation of workflow invocations (``path`` is prefixed
           with the parent's node name).

        Steps 1–3 are already enforced by ``Node.__init__`` during
        construction; this method exists so GUIs that built the workflow
        via :meth:`capture_errors` / :meth:`from_dict` can re-check after
        the fact. Step 4 (constant Pydantic validation) only runs here —
        it is intentionally not performed at construction time, so a GUI
        editing one field at a time does not need every other field to
        be valid yet.

        Parameters
        ----------
        dev_mode
            Accepted for symmetry with :meth:`plan`; unused by validate.

        Returns
        -------
        list[ValidationError]
            Deduplicated, sorted by (path, node, field, kind).
        """
        from bioimageflow.engine import topological_order
        from bioimageflow.validation import (
            check_type_compat,
            validate_parameters,
        )
        from bioimageflow.workflow_node import WorkflowNode
        from graphlib import CycleError

        if id(self) in _recursion_stack:
            return [ValidationError(
                kind="construction_failed",
                message=(
                    "Recursive workflow containment is not allowed on one "
                    "active definition path."
                ),
            )]
        recursion_stack = (*_recursion_stack, id(self))
        errors: list[ValidationError] = []

        if not self.name or "/" in self.name or not isinstance(self.display_name, str):
            errors.append(ValidationError(
                kind="construction_failed",
                message="Invalid workflow definition metadata.",
            ))

        interface_ids: set[str] = set()
        interface_names: set[str] = set()
        for port in [*self._interface_inputs.values(), *self._interface_outputs.values()]:
            if (
                not port.id
                or port.id in interface_ids
                or not port.name
                or port.name in interface_names
                or port.name == "name"
            ):
                errors.append(ValidationError(
                    kind="duplicate_name",
                    message=f"Workflow interface ID or name is not unique: '{port.name}'.",
                    field=port.name,
                ))
            interface_ids.add(port.id)
            interface_names.add(port.name)

        for port in self._interface_inputs.values():
            for target in port.targets:
                target_name = target.get("node")
                target_node = (
                    self._nodes.get(target_name)
                    if isinstance(target_name, str)
                    else None
                )
                endpoint = target.get("port")
                if target_node is None or not isinstance(endpoint, dict):
                    errors.append(ValidationError(
                        kind="missing_input",
                        message=f"Workflow input '{port.name}' has an unknown target.",
                        field=port.name,
                    ))
                    continue
                endpoint_kind = endpoint.get("kind")
                if endpoint_kind != "workflow" and (
                    (port.kind == "field") != (endpoint_kind == "field")
                ):
                    errors.append(ValidationError(
                        kind="type_mismatch",
                        message=f"Workflow input '{port.name}' has an incompatible target kind.",
                        node=target_node.name,
                        field=port.name,
                    ))
                if endpoint_kind == "workflow":
                    from bioimageflow.workflow_node import WorkflowNode

                    child_id = endpoint.get("id")
                    child_port = None
                    if isinstance(target_node, WorkflowNode) and isinstance(child_id, str):
                        child_port = target_node.workflow._interface_inputs.get(child_id)
                    if child_port is None or child_port.kind != port.kind:
                        errors.append(ValidationError(
                            kind="type_mismatch",
                            message=f"Workflow input '{port.name}' has an incompatible child port.",
                            node=target_node.name,
                            field=port.name,
                        ))

        for port in self._interface_outputs.values():
            source_node = self._nodes.get(port.source_node)
            if source_node is None:
                errors.append(ValidationError(
                    kind="column_not_found",
                    message=f"Workflow output '{port.name}' has an unknown source node.",
                    field=port.name,
                ))
                continue
            output_schema = source_node.get_output_schema()
            if output_schema is not None and port.source_output not in output_schema:
                errors.append(ValidationError(
                    kind="column_not_found",
                    message=f"Workflow output '{port.name}' has an unknown source column.",
                    node=source_node.name,
                    field=port.name,
                ))

        for registered_name, registered_node in self._nodes.items():
            if (
                not registered_name
                or "/" in registered_name
                or registered_node._name != registered_name
            ):
                errors.append(ValidationError(
                    kind="duplicate_name",
                    message=f"Invalid structural node identity '{registered_name}'.",
                    node=registered_name,
                ))

        # Step 1: cycle detection (doesn't block the rest — we still check parameters).
        try:
            topological_order(self)
        except CycleError as exc:
            cycle = exc.args[1] if len(exc.args) > 1 else []
            errors.append(ValidationError(
                kind="cycle",
                message=f"Cycle detected: {cycle}",
            ))

        for name, node in self._nodes.items():
            if isinstance(node, WorkflowNode):
                unknown_ports = node._bound_port_ids() - set(
                    node.workflow._interface_inputs
                )
                for port_id in unknown_ports:
                    errors.append(ValidationError(
                        kind="unknown_input",
                        message=f"Unknown workflow input port '{port_id}'.",
                        node=name,
                        field=port_id,
                    ))
                for port in node.workflow._interface_inputs.values():
                    if port.id not in node._bound_port_ids() and not port.has_fallback(node.workflow):
                        errors.append(ValidationError(
                            kind="missing_input",
                            message=(
                                f"Missing required input '{port.name}' for workflow "
                                f"'{node.workflow.name}'."
                            ),
                            node=name,
                            field=port.name,
                        ))
                for e in node.workflow.validate(_recursion_stack=recursion_stack):
                    errors.append(ValidationError(
                        kind=e.kind,
                        message=e.message,
                        node=e.node,
                        field=e.field,
                        edge=e.edge,
                        edge_id=e.edge_id,
                        path=(name, *e.path),
                    ))
                continue

            # Step 2: type compatibility on column bindings.
            for field, col_ref in node._column_bindings.items():
                eid = node._column_binding_edge_ids.get(field)
                err = check_type_compat(node, field, col_ref)
                if err is not None:
                    if eid is not None and err.edge_id is None:
                        err = ValidationError(
                            kind=err.kind, message=err.message, node=err.node,
                            field=err.field, edge=err.edge, edge_id=eid,
                            path=err.path,
                        )
                    errors.append(err)
                # Column-not-found on bindings recorded at the structural level.
                upstream_outputs = col_ref.node.tool.Outputs
                if upstream_outputs is not None:
                    from bioimageflow.dataframe_tool import Passthrough
                    output_annotations = upstream_outputs._get_all_annotations()
                    if not issubclass(upstream_outputs, Passthrough) and \
                            col_ref.column not in output_annotations:
                        errors.append(ValidationError(
                            kind="column_not_found",
                            message=(
                                f"Column '{col_ref.column}' not found in "
                                f"outputs of node '{col_ref.node.name}'. "
                                f"Available: {list(output_annotations.keys())}"
                            ),
                            node=name,
                            field=field,
                            edge=(col_ref.node.name, name, field),
                            edge_id=eid,
                        ))

            # Step 3: missing required inputs.
            input_annotations = node.tool.Inputs._get_all_annotations()
            for field_name in input_annotations:
                if field_name in node._column_bindings:
                    continue
                if field_name in node._constant_bindings:
                    continue
                if field_name in node._workflow_input_bindings:
                    continue
                if hasattr(node.tool.Inputs, field_name):
                    continue
                errors.append(ValidationError(
                    kind="missing_input",
                    message=(
                        f"Missing required input '{field_name}' for tool "
                        f"'{type(node.tool).__name__}'."
                    ),
                    node=name,
                    field=field_name,
                ))

            # Step 4: Pydantic validation of supplied constants.
            try:
                param_errors = validate_parameters(
                    type(node.tool), node._constant_bindings, node=name,
                )
            except Exception as exc:  # pragma: no cover — defensive
                param_errors = [ValidationError(
                    kind="construction_failed",
                    message=f"Pydantic validation setup failed: {exc}",
                    node=name,
                )]
            errors.extend(param_errors)

        # Deduplicate + sort for determinism.
        seen: set[tuple[Any, ...]] = set()
        unique: list[ValidationError] = []
        for e in errors:
            key = (e.path, e.node, e.field, e.kind, e.message, e.edge_id)
            if key in seen:
                continue
            seen.add(key)
            unique.append(e)
        unique.sort(key=lambda e: (e.path, e.node or "", e.field or "", e.kind))
        return unique

    @contextmanager
    def capture_errors(self) -> Iterator[list[ValidationError]]:
        """Capture node-construction errors as :class:`ValidationError`.

        Usage::

            wf = Workflow()
            with wf, wf.capture_errors() as errors:
                MyTool()(input=upstream["bad_col"])
            # errors: list[ValidationError]

        Nested blocks push their own list; the outer list is restored on
        exit. Disables the "raise on first error" behavior of ``Node``
        construction only for the duration of the block.
        """
        errs: list[ValidationError] = []
        token = _error_capture.set(errs)
        try:
            yield errs
        finally:
            _error_capture.reset(token)

    def _register_node(self, node: Node) -> None:
        """Register a node with this workflow."""
        self._nodes[node.name] = node

    @property
    def nodes(self) -> dict[str, Node]:
        return dict(self._nodes)

    @property
    def errors(self) -> list[ValidationError]:
        """Build-time errors accumulated during :meth:`from_dict`.

        Empty when the workflow was constructed programmatically (via
        the context-manager / call-tools pattern) or when ``from_dict``
        was called in strict mode.
        """
        return list(self._build_errors)

    @property
    def failed_nodes(self) -> dict[str, ValidationError]:
        """Map of node name → :class:`ValidationError` for nodes that
        failed to construct during :meth:`from_dict`.

        Populated only when ``from_dict`` is called with ``partial=True``
        and a node's tool resolution or construction raised. Empty
        otherwise.
        """
        return dict(self._failed_nodes)

    @property
    def is_partial(self) -> bool:
        """Whether the workflow is missing nodes that the input dict
        described.

        ``True`` when at least one entry in the source ``data["nodes"]``
        is absent from :attr:`nodes` (typically because it failed to
        construct in collect mode). ``False`` for fully-built workflows
        and for workflows constructed without :meth:`from_dict`.
        """
        if self._expected_node_names is None:
            return False
        return not self._expected_node_names.issubset(self._nodes.keys())

    def disable(self, *nodes: "Node | str") -> None:
        """Disable nodes by reference or name."""
        for item in nodes:
            node = self._resolve_node(item)
            node.enabled = False

    def enable(self, *nodes: "Node | str") -> None:
        """Enable nodes by reference or name."""
        for item in nodes:
            node = self._resolve_node(item)
            node.enabled = True

    def _resolve_node(self, item: "Node | str") -> Node:
        """Resolve a node reference or name to a Node object."""
        if isinstance(item, str):
            if item not in self._nodes:
                raise KeyError(
                    f"Node '{item}' not found in workflow. "
                    f"Available nodes: {list(self._nodes.keys())}"
                )
            return self._nodes[item]
        return item

    def cancel(self) -> None:
        """Request cancellation of the running workflow."""
        self._cancel_event.set()

    @property
    def cancel_requested(self) -> bool:
        """Whether cancellation has been requested."""
        return self._cancel_event.is_set()

    def get_environment(self, target: "ProcessingTool | EnvironmentSpec | str") -> WorkflowEnvironment:
        """Get the launch configuration proxy for an environment.

        Args:
            target: A ProcessingTool instance, an EnvironmentSpec, or an env name string.

        Returns:
            A shared WorkflowEnvironment proxy. Multiple calls with tools sharing
            the same environment return the same object.
        """
        if isinstance(target, str):
            name = target
            spec = None
        elif isinstance(target, EnvironmentSpec):
            name = target.name
            spec = target
        elif isinstance(target, ProcessingTool):
            name = target.environment.name
            spec = target.environment
        else:
            raise TypeError(
                f"Expected a ProcessingTool, EnvironmentSpec, or str, got {type(target).__name__}"
            )
        if name not in self._env_configs:
            self._env_configs[name] = WorkflowEnvironment(name=name, spec=spec)
        elif spec is not None and self._env_configs[name].spec is None:
            self._env_configs[name].spec = spec
        return self._env_configs[name]

    def compute(
        self,
        *targets: Node,
        inputs: Mapping[str, Any] | None = None,
        dev_mode: bool = False,
        engine: "DefaultEngine | None" = None,
    ) -> Any:
        """Execute the workflow and return results.

        Parameters:
            dev_mode: Development mode flag
            engine: Optional pre-configured engine to use. If None, the configured engine backend and execution policy are used. Providing an engine allows post-execution inspection and testing."""
        self._cancel_event.clear()
        self._dev_mode = dev_mode

        if inputs is not None or not targets:
            supplied = dict(inputs or {})
            parent = Workflow(
                name=f"{self.name}-execution",
                storage_path=self.storage_path,
                engine=self.engine_type,
                execution=self.execution,
                on_progress=self.on_progress,
                wetlands_config=self.wetlands_config,
                max_workers=self.max_workers,
                output_view=self.output_view,
            )
            parent._accept_root_dataframes = True
            with parent:
                boundary = self(name=self.name, **supplied)
            boundary._is_root_boundary = True
            return parent.compute(boundary, dev_mode=dev_mode, engine=engine)

        # If targets are not registered (explicit workflow without context manager),
        # discover the graph by tracing upstream
        target_list = list(targets)
        self._discover_graph(target_list)

        if engine is None:
            engine = self._make_engine()
        self._start_run_view(target_list)
        try:
            results = engine.execute(target_list, self)
        except Exception as exc:
            self._finish_run_view(
                self._run_status_for_exception(exc),
                update_latest_success=False,
            )
            raise
        else:
            self._finish_run_view("succeeded", update_latest_success=True)

        if len(target_list) == 1:
            return list(results.values())[0]
        return results

    def compute_steps(
        self,
        *targets: Node,
        inputs: Mapping[str, Any] | None = None,
        dev_mode: bool = False,
        engine: "DefaultEngine | None" = None,
    ) -> "Generator[NodeStep, None, None]":
        """Execute the workflow step by step, yielding a :class:`NodeStep`
        for each node in topological (dependency) order.

        Parameters:
            dev_mode: Development mode flag
            engine: Optional pre-configured engine to use. If None, a default DefaultEngine is created.

        The engine stays alive between yields so Wetlands environments
        remain warm — ideal for interactive debugging.

        Usage::

            for step in wf.compute_steps(results):
                print(f"Next: {step.node_name}")
                step.prepare()     # optional: launches env — attach debugger here
                df = step.execute()
                print(df.head())

        If ``step.execute()`` is not called before advancing to the next
        iteration, the step auto-executes to keep downstream nodes consistent.
        """
        self._dev_mode = dev_mode

        if inputs is not None or not targets:
            supplied = dict(inputs or {})
            parent = Workflow(
                name=f"{self.name}-execution",
                storage_path=self.storage_path,
                engine=self.engine_type,
                execution=self.execution,
                on_progress=self.on_progress,
                wetlands_config=self.wetlands_config,
                max_workers=self.max_workers,
                output_view=self.output_view,
            )
            parent._accept_root_dataframes = True
            with parent:
                boundary = self(name=self.name, **supplied)
            boundary._is_root_boundary = True
            yield from parent.compute_steps(
                boundary,
                dev_mode=dev_mode,
                engine=engine,
            )
            return

        target_list = list(targets)
        self._discover_graph(target_list)

        if engine is None:
            engine = self._make_engine()
        self._start_run_view(target_list)
        try:
            yield from engine.execute_steps(target_list, self)
        except GeneratorExit:
            self._finish_run_view("cancelled", update_latest_success=False)
            raise
        except Exception as exc:
            self._finish_run_view(
                self._run_status_for_exception(exc),
                update_latest_success=False,
            )
            raise
        else:
            self._finish_run_view("succeeded", update_latest_success=True)

    def export_outputs(
        self,
        *,
        mode: Literal["symlink", "copy", "hardlink"] = "symlink",
        scope: Literal["latest", "runs", "both"] = "latest",
        run_id: str | None = None,
    ) -> list[Path]:
        """Materialize human-facing output files from the portable JSON views."""
        if scope not in {"latest", "runs", "both"}:
            raise ValueError("Invalid output_view scope. Expected 'latest', 'runs', or 'both'.")
        from bioimageflow.storage import CacheCorruptionError, Storage

        storage = Storage(self.storage_path)
        materialized: list[Path] = []
        if scope in {"latest", "both"}:
            materialized.extend(storage.materialize_latest_outputs(mode))
        if scope in {"runs", "both"}:
            selected_run_id = run_id or storage.latest_success_run_id()
            if selected_run_id is None:
                raise CacheCorruptionError("No successful run view is available for output export.")
            materialized.extend(storage.materialize_run_outputs(selected_run_id, mode))
        return materialized

    def _auto_export_outputs(
        self,
        run_id: str,
        *,
        latest_node: str | None = None,
        runs: bool,
    ) -> None:
        output_view = self.output_view
        if output_view is None or output_view.mode == "none":
            return
        from bioimageflow.storage import Storage

        storage = Storage(self.storage_path)
        if latest_node is not None and output_view.scope in {"latest", "both"}:
            storage.materialize_latest_node_outputs(latest_node, output_view.mode)
        if runs and output_view.scope in {"runs", "both"}:
            storage.materialize_run_outputs(run_id, output_view.mode)

    def _start_run_view(self, targets: list[Node]) -> None:
        from bioimageflow.storage import Storage

        storage = Storage(self.storage_path)
        run_id = f"run_{storage.new_attempt_id()}"
        started_at = datetime.now(timezone.utc).isoformat()
        target_nodes = [target.name for target in targets]
        self._run_view_context = {
            "run_id": run_id,
            "started_at": started_at,
            "target_nodes": target_nodes,
        }
        storage.write_run_metadata(
            run_id,
            workflow_identity=self._workflow_identity(target_nodes),
            engine=f"{self.engine_type}:{self.execution}",
            status="running",
            target_nodes=target_nodes,
            started_at=started_at,
        )

    def _finish_run_view(self, status: str, *, update_latest_success: bool) -> None:
        context = self._run_view_context
        self._run_view_context = None
        if context is None:
            return
        from bioimageflow.storage import Storage

        storage = Storage(self.storage_path)
        run_id = str(context["run_id"])
        storage.write_run_metadata(
            run_id,
            workflow_identity=self._workflow_identity(list(context["target_nodes"])),
            engine=f"{self.engine_type}:{self.execution}",
            status=status,
            target_nodes=list(context["target_nodes"]),
            started_at=str(context["started_at"]),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        if update_latest_success:
            storage.update_latest_success_run(run_id)
        if status == "succeeded":
            self._auto_export_outputs(run_id, latest_node=None, runs=True)

    def _workflow_identity(self, target_nodes: list[str]) -> str:
        payload = {
            "nodes": sorted(self._nodes),
            "targets": list(target_nodes),
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return f"workflow:{digest}"

    def _run_status_for_exception(self, exc: Exception) -> str:
        from bioimageflow.engine import WorkflowCancelledError

        return "cancelled" if isinstance(exc, WorkflowCancelledError) else "failed"

    def _discover_graph(self, targets: list[Node]) -> None:
        """Discover and register all nodes reachable from targets."""
        visited: set[str] = set()
        queue = list(targets)
        while queue:
            node = queue.pop(0)
            if node.name in visited:
                continue
            visited.add(node.name)
            if node.name not in self._nodes:
                self._nodes[node.name] = node
            for up in node._upstream_nodes:
                queue.append(up)
            for arg in node._args:
                if isinstance(arg, Node):
                    queue.append(arg)

    def to_dict(self, *, include_custom_tools: bool = False) -> dict[str, Any]:
        """Serialize the strict recursive schema-version-1 graph."""
        sources: list[dict[str, Any]] = copy.deepcopy(self._captured_custom_sources or [])
        source_ids: set[str] = {record["id"] for record in sources}
        graph = self._graph_to_dict(
            include_custom_tools=include_custom_tools,
            sources=sources,
            source_ids=source_ids,
        )
        if include_custom_tools and sources:
            return {"archive_version": 1, "workflow": graph, "custom_sources": sources}
        return graph

    def _graph_to_dict(
        self,
        *,
        include_custom_tools: bool,
        sources: list[dict[str, Any]],
        source_ids: set[str],
    ) -> dict[str, Any]:
        from bioimageflow.workflow_node import WorkflowNode
        from bioimageflow.tool_loader import get_tool_package_info

        nodes_data: list[dict[str, Any]] = []
        edges_data: list[dict[str, Any]] = []

        def edge_id(material: str, explicit: str | None = None) -> str:
            if explicit:
                return explicit
            return f"edge-{hashlib.sha256(material.encode()).hexdigest()[:20]}"

        for name, node in self._nodes.items():
            if isinstance(node, WorkflowNode):
                node_info = {
                    "name": name,
                    "type": "workflow",
                    "workflow": node.workflow._graph_to_dict(
                        include_custom_tools=include_custom_tools,
                        sources=sources,
                        source_ids=source_ids,
                    ),
                    "bindings": {
                        port_id: serialize_constant(value)
                        for port_id, value in node._input_constant_bindings.items()
                    },
                }
                if not node.enabled:
                    node_info["enabled"] = False
                nodes_data.append(node_info)

                for port_id, col_ref in node._input_column_bindings.items():
                    explicit = node._input_column_binding_edge_ids.get(port_id)
                    edges_data.append({
                        "type": "column",
                        "id": edge_id(f"column:{col_ref.node.name}:{col_ref.column}:{name}:{port_id}", explicit),
                        "source_node": col_ref.node.name,
                        "source_output": col_ref.column,
                        "target_node": name,
                        "target_input": port_id,
                    })
                for port_id, upstream in node._input_dataframe_bindings.items():
                    explicit = node._input_dataframe_binding_edge_ids.get(port_id)
                    edges_data.append({
                        "type": "dataframe",
                        "id": edge_id(f"dataframe:{upstream.name}:{name}:{port_id}", explicit),
                        "source_node": upstream.name,
                        "target_node": name,
                        "target_input": port_id,
                    })
            else:
                pkg, pkg_ver, canonical_module = get_tool_package_info(node.tool)
                node_info = {
                    "name": name,
                    "type": "tool",
                    "tool_module": canonical_module,
                    "tool_class": type(node.tool).__name__,
                    "tool_package": pkg,
                    "tool_package_version": pkg_ver,
                    "constants": {
                        field: serialize_constant(value)
                        for field, value in node._constant_bindings.items()
                        if field not in node._workflow_input_bindings
                        or field in node._workflow_input_fallback_constants
                    }
                }
                if include_custom_tools:
                    source_id = _register_custom_tool_module(
                        type(node.tool),
                        records=sources,
                        seen_ids=source_ids,
                    )
                    if source_id is not None:
                        node_info["source_module"] = source_id
                if not node.enabled:
                    node_info["enabled"] = False
                if node.output_templates:
                    node_info["output_templates"] = dict(node.output_templates)
                nodes_data.append(node_info)

                for field, col_ref in node._column_bindings.items():
                    if field in node._workflow_input_bindings:
                        continue
                    explicit = node._column_binding_edge_ids.get(field)
                    edges_data.append({
                        "type": "column",
                        "id": edge_id(f"column:{col_ref.node.name}:{col_ref.column}:{name}:{field}", explicit),
                        "source_node": col_ref.node.name,
                        "source_output": col_ref.column,
                        "target_node": name,
                        "target_input": field,
                    })
                for idx, arg in enumerate(node._args):
                    if isinstance(arg, Node) and idx not in node._workflow_dataframe_bindings:
                        explicit = (
                            node._arg_edge_ids[idx]
                            if idx < len(node._arg_edge_ids)
                            else None
                        )
                        edges_data.append({
                            "type": "dataframe",
                            "id": edge_id(f"dataframe:{arg.name}:{name}:{idx}", explicit),
                            "source_node": arg.name,
                            "target_node": name,
                            "target_position": idx,
                        })

        result = {
            "schema_version": 1,
            "name": self.name,
            "display_name": self.display_name,
            "interface": {
                "inputs": [
                    {
                        "id": port.id,
                        "name": port.name,
                        "kind": port.kind,
                        **({"schema": copy.deepcopy(port.schema)} if port.schema is not None else {}),
                        **({"default": serialize_constant(port.default)} if port.default is not MISSING else {}),
                        "targets": copy.deepcopy(port.targets),
                    }
                    for port in self._interface_inputs.values()
                ],
                "outputs": [
                    {
                        "id": port.id,
                        "name": port.name,
                        **({"schema": copy.deepcopy(port.schema)} if port.schema is not None else {}),
                        "source": {"node": port.source_node, "column": port.source_output},
                    }
                    for port in self._interface_outputs.values()
                ],
            },
            "nodes": nodes_data,
            "edges": edges_data,
            "config": {
                "storage_path": self._storage_path_config,
                "engine": self.engine_type,
                "execution": self.execution,
            },
        }
        if self.output_view is not None:
            result["config"]["output_view"] = self.output_view.to_dict()
        return result

    def export(self, path: str | Path) -> None:
        """Serialize the workflow to a JSON file or BioImageFlow zip archive."""
        path = Path(path)
        if path.suffix == ".zip":
            self._export_archive(path)
            return
        data = self.to_dict(include_custom_tools=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str))

    def _export_archive(self, path: Path) -> None:
        data = self.to_dict(include_custom_tools=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("workflow.json", json.dumps(data, indent=2, default=str))

    @classmethod
    def load(cls, path: str | Path) -> "Workflow":
        """Deserialize a workflow from a JSON file.

        Thin wrapper around :meth:`from_dict`. Preserves the original
        behavior: raises on the first error, auto-installs missing
        versioned packages.
        """
        path = Path(path)
        if path.suffix == ".zip":
            return cls._load_archive(path)
        data = json.loads(path.read_text())
        result = cls.from_dict(data)
        assert isinstance(result, Workflow)  # strict mode
        return result

    @classmethod
    def from_python(cls, path_or_module: str | Path | Any) -> "Workflow":
        """Execute a trusted module's exact ``build_workflow`` factory once."""
        import types

        cleanup_callback: Callable[[], None] | None
        if isinstance(path_or_module, types.ModuleType):
            module = path_or_module
            cleanup_callback = None
        else:
            candidate = Path(path_or_module) if isinstance(path_or_module, (str, Path)) else None
            if candidate is not None and candidate.suffix == ".py" and candidate.exists():
                source_path = candidate.resolve()
                source_root = source_path.parent
                captured = {
                    path.relative_to(source_root): path.read_bytes()
                    for path in source_root.rglob("*.py")
                    if "__pycache__" not in path.parts
                }
                temp_root = Path(tempfile.mkdtemp(prefix="bioimageflow_python_definition_"))
                for relative, content in captured.items():
                    destination = temp_root / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(content)
                entry = temp_root / source_path.relative_to(source_root)
                module_name = f"_bioimageflow_definition_{uuid.uuid4().hex}"
                spec = importlib.util.spec_from_file_location(module_name, entry)
                if spec is None or spec.loader is None:
                    raise ImportError(f"Cannot load workflow definition from '{source_path}'.")
                module = importlib.util.module_from_spec(spec)
                old_path = list(sys.path)
                previous_local = {
                    name: loaded
                    for name, loaded in sys.modules.items()
                    if getattr(loaded, "__file__", None)
                    and _path_is_within(Path(cast(str, loaded.__file__)), source_root)
                }
                for name in previous_local:
                    sys.modules.pop(name, None)
                sys.modules[module_name] = module
                sys.path.insert(0, str(temp_root))

                def cleanup_file_import() -> None:
                    sys.path[:] = old_path
                    for name, loaded in list(sys.modules.items()):
                        loaded_file = getattr(loaded, "__file__", None)
                        if loaded_file and _path_is_within(Path(loaded_file), temp_root):
                            sys.modules.pop(name, None)
                    sys.modules.update(previous_local)

                cleanup_callback = cleanup_file_import

                try:
                    spec.loader.exec_module(module)
                except Exception:
                    cleanup_callback()
                    raise
            elif isinstance(path_or_module, str):
                module = importlib.import_module(path_or_module)
                cleanup_callback = None
            else:
                raise TypeError("from_python expects a module, import name, or Python file path.")

        try:
            factory = getattr(module, "build_workflow")
            if not callable(factory):
                raise TypeError("build_workflow must be callable.")
            workflow = factory()
            if not isinstance(workflow, cls):
                raise TypeError("build_workflow must return a Workflow and nothing else.")
            if get_active_workflow() is workflow:
                raise RuntimeError("build_workflow returned with its Workflow still active.")
            captured_export = workflow.to_dict(include_custom_tools=True)
            if "custom_sources" in captured_export:
                workflow._captured_custom_sources = copy.deepcopy(captured_export["custom_sources"])
            return workflow
        finally:
            if cleanup_callback is not None:
                cleanup_callback()

    @classmethod
    def _load_archive(cls, path: Path) -> "Workflow":
        temp_root = Path(tempfile.mkdtemp(prefix="bioimageflow_workflow_archive_"))
        _extract_workflow_archive(path, temp_root)
        workflow_path = temp_root / "workflow.json"
        if not workflow_path.exists():
            raise ValueError("Workflow archive is missing workflow.json")
        with _workflow_import_scope(temp_root):
            data = json.loads(workflow_path.read_text(encoding="utf-8"))
            result = cls.from_dict(data)
            assert isinstance(result, Workflow)  # strict mode
            return result

    @classmethod
    def import_archive(cls, path: str | Path, destination: str | Path) -> "Workflow":
        """Extract a BioImageFlow zip archive to ``destination`` and load it."""
        path = Path(path)
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        _extract_workflow_archive(path, destination)
        workflow_path = destination / "workflow.json"
        if not workflow_path.exists():
            raise ValueError("Workflow archive is missing workflow.json")
        with _workflow_import_scope(destination):
            data = json.loads(workflow_path.read_text(encoding="utf-8"))
            result = cls.from_dict(data)
            assert isinstance(result, Workflow)  # strict mode
            return result

    @overload
    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        validate_only: Literal[True],
        partial: bool = False,
        auto_install: bool = True,
        storage_path_override: str | Path | None = None,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        engine: str | None = None,
        execution: str | None = None,
        wetlands_config: dict[str, Any] | None = None,
    ) -> "tuple[Workflow, list[ValidationError]]": ...

    @overload
    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        validate_only: Literal[False] = False,
        partial: bool = False,
        auto_install: bool = True,
        storage_path_override: str | Path | None = None,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        engine: str | None = None,
        execution: str | None = None,
        wetlands_config: dict[str, Any] | None = None,
    ) -> "Workflow": ...

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        validate_only: bool = False,
        partial: bool = False,
        auto_install: bool = True,
        storage_path_override: str | Path | None = None,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        engine: str | None = None,
        execution: str | None = None,
        wetlands_config: dict[str, Any] | None = None,
    ) -> "Workflow | tuple[Workflow, list[ValidationError]]":
        """Reconstruct a Workflow from a serialized dict.

        Parameters
        ----------
        data
            A schema-version-1 recursive graph produced by :meth:`to_dict`,
            or a portable archive envelope produced by :meth:`export`.
        validate_only
            Drives the **return type**. When ``True``, returns a
            ``(workflow, errors)`` tuple; the workflow may be partial.
            When ``False`` (default), returns the ``Workflow`` directly
            and aggregates any captured errors into a raised exception.
        partial
            Drives **error suppression / continuation**. When ``True``,
            per-node failures are captured as :class:`ValidationError`
            entries and construction continues; the workflow may be
            best-effort partially wired. When ``False`` (default),
            construction stops at the first failure.
        auto_install
            When True (default), missing versioned packages are installed
            automatically. When False, missing packages produce an
            ``unknown_tool`` error (when captured) or raise.
        storage_path_override
            Override ``data["config"]["storage_path"]`` without mutating
            the dict. Useful for GUIs that validate a graph against a
            specific cache path.
        on_progress, engine, execution, wetlands_config
            Passed to :class:`Workflow`. ``None`` means "use the values
            from ``data['config']`` (or defaults)".

        Notes
        -----
        The ``partial=False, validate_only=True`` combination returns a
        ``(workflow, errors)`` tuple where ``errors`` contains at most
        one entry (the first failure) and the workflow may be empty —
        useful as a fail-fast diagnostic.
        """
        errors: list[ValidationError] = []
        try:
            wf = cls._from_recursive_dict(
                data,
                auto_install=auto_install,
                storage_path_override=storage_path_override,
                on_progress=on_progress,
                engine=engine,
                execution=execution,
                wetlands_config=wetlands_config,
                partial=partial,
                errors=errors,
            )
        except Exception as exc:
            if not validate_only:
                if partial and errors:
                    raise ValueError(
                        f"Workflow construction failed with {len(errors)} error(s)."
                    ) from exc
                raise
            errors.append(ValidationError(kind="construction_failed", message=str(exc)))
            wf = cls(
                storage_path=storage_path_override or "./bif_data",
                engine=engine or "wetlands",
                execution=execution or "parallel",
            )
        wf._build_errors = list(errors)
        if validate_only:
            return wf, errors
        if errors:
            raise ValueError(
                f"Workflow construction failed with {len(errors)} error(s)."
            )
        return wf

    @classmethod
    def _from_recursive_dict(
        cls,
        data: dict[str, Any],
        *,
        auto_install: bool,
        storage_path_override: str | Path | None,
        on_progress: Callable[[ProgressEvent], None] | None,
        engine: str | None,
        execution: str | None,
        wetlands_config: dict[str, Any] | None,
        partial: bool = False,
        errors: list[ValidationError] | None = None,
    ) -> "Workflow":
        """Materialize a strict graph or portable archive envelope."""
        if set(data) == {"archive_version", "workflow", "custom_sources"}:
            if data["archive_version"] != 1 or not isinstance(data["custom_sources"], list):
                raise ValueError("Unsupported workflow archive envelope.")
            graph = data["workflow"]
            source_records = data["custom_sources"]
        else:
            graph = data
            source_records = []
        if not isinstance(graph, dict):
            raise TypeError("Workflow graph must be a dictionary.")
        custom_modules = _load_custom_sources(source_records)
        return cls._materialize_graph(
            graph,
            custom_modules=custom_modules,
            source_records=source_records,
            auto_install=auto_install,
            storage_path_override=storage_path_override,
            on_progress=on_progress,
            engine=engine,
            execution=execution,
            wetlands_config=wetlands_config,
            partial=partial,
            errors=errors,
        )

    @classmethod
    def _materialize_graph(
        cls,
        graph: dict[str, Any],
        *,
        custom_modules: dict[str, Any],
        source_records: list[dict[str, Any]],
        auto_install: bool,
        storage_path_override: str | Path | None = None,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        engine: str | None = None,
        execution: str | None = None,
        wetlands_config: dict[str, Any] | None = None,
        partial: bool = False,
        errors: list[ValidationError] | None = None,
        graph_stack: tuple[int, ...] = (),
    ) -> "Workflow":
        from graphlib import TopologicalSorter
        from bioimageflow.dataframe_tool import DataFrameTool
        if id(graph) in graph_stack:
            raise ValueError("Recursive workflow graph containment is not allowed.")
        graph_stack = (*graph_stack, id(graph))
        required = {"schema_version", "name", "display_name", "interface", "nodes", "edges", "config"}
        if set(graph) != required:
            raise ValueError(
                f"Workflow graph fields must be exactly {sorted(required)}; got {sorted(graph)}."
            )
        if graph["schema_version"] != 1:
            raise ValueError("Only workflow schema_version 1 is supported.")
        if (
            not isinstance(graph["name"], str)
            or not graph["name"]
            or "/" in graph["name"]
            or not isinstance(graph["display_name"], str)
        ):
            raise ValueError("Invalid workflow definition metadata.")
        if not isinstance(graph["interface"], dict) or set(graph["interface"]) != {"inputs", "outputs"}:
            raise ValueError("Workflow interface must contain exactly 'inputs' and 'outputs'.")
        if not isinstance(graph["nodes"], list) or not isinstance(graph["edges"], list):
            raise ValueError("Workflow nodes and edges must be arrays.")
        if not all(isinstance(items, list) for items in graph["interface"].values()):
            raise ValueError("Workflow interface inputs and outputs must be arrays.")
        config = graph["config"]
        if not isinstance(config, dict) or not set(config) <= {
            "storage_path", "engine", "execution", "output_view"
        }:
            raise ValueError("Unknown workflow config field.")
        wf = cls(
            name=graph["name"],
            display_name=graph["display_name"],
            storage_path=storage_path_override or config.get("storage_path", "./bif_data"),
            engine=engine or config.get("engine", "wetlands"),
            execution=execution or config.get("execution", "parallel"),
            output_view=config.get("output_view"),
            on_progress=on_progress,
            wetlands_config=wetlands_config,
        )
        wf._captured_custom_sources = copy.deepcopy(source_records)
        wf._expected_node_names = {
            cast(str, item.get("name"))
            for item in graph["nodes"]
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }

        serialized_targets: dict[str, list[dict[str, Any]]] = {}
        for item in graph["interface"]["inputs"]:
            allowed = {"id", "name", "kind", "schema", "default", "targets"}
            if not isinstance(item, dict) or not {"id", "name", "kind", "targets"} <= set(item) or not set(item) <= allowed:
                raise ValueError("Malformed workflow input record.")
            if (
                not isinstance(item["id"], str)
                or not item["id"]
                or not isinstance(item["name"], str)
                or not item["name"]
                or item["name"] == "name"
                or item["kind"] not in {"field", "dataframe"}
                or not isinstance(item["targets"], list)
            ):
                raise ValueError("Invalid workflow input name or kind.")
            port = WorkflowInputPort(
                id=item["id"],
                name=item["name"],
                kind=item["kind"],
                annotation=Any,
                schema=copy.deepcopy(item.get("schema")),
                default=deserialize_constant(item["default"]) if "default" in item else MISSING,
            )
            if port.id in wf._interface_inputs or any(p.name == port.name for p in wf._interface_inputs.values()):
                raise ValueError("Duplicate workflow input ID or name.")
            wf._interface_inputs[port.id] = port
            serialized_targets[port.id] = copy.deepcopy(item["targets"])

        nodes_by_name: dict[str, dict[str, Any]] = {}
        for node_data in graph["nodes"]:
            if not isinstance(node_data, dict) or node_data.get("type") not in {"tool", "workflow"}:
                raise ValueError("Unknown or malformed workflow node variant.")
            name = node_data.get("name")
            if not isinstance(name, str) or not name or "/" in name or name in nodes_by_name:
                raise ValueError("Node names must be unique, non-empty, and may not contain '/'.")
            required_node_fields = (
                {"name", "type", "workflow", "bindings"}
                if node_data["type"] == "workflow"
                else {
                    "name", "type", "tool_module", "tool_class", "tool_package",
                    "tool_package_version", "constants",
                }
            )
            allowed = (
                {"name", "type", "workflow", "bindings", "enabled"}
                if node_data["type"] == "workflow"
                else {
                    "name", "type", "tool_module", "tool_class", "tool_package",
                    "tool_package_version", "source_module", "constants",
                    "output_templates", "enabled",
                }
            )
            if not required_node_fields <= set(node_data) or not set(node_data) <= allowed:
                raise ValueError(f"Malformed or unknown fields on node '{name}'.")
            nodes_by_name[name] = node_data

        incoming: dict[str, list[dict[str, Any]]] = {name: [] for name in nodes_by_name}
        deps: dict[str, set[str]] = {name: set() for name in nodes_by_name}
        edge_ids: set[str] = set()
        for edge in graph["edges"]:
            if not isinstance(edge, dict) or edge.get("type") not in {"column", "dataframe"}:
                raise ValueError("Unknown or malformed edge variant.")
            common = {"type", "id", "source_node", "target_node"}
            allowed = common | (
                {"source_output", "target_input"}
                if edge["type"] == "column"
                else {"target_position", "target_input"}
            )
            if not set(edge) <= allowed or not common <= set(edge):
                raise ValueError("Malformed edge endpoint combination.")
            if edge["type"] == "column" and set(edge) != common | {"source_output", "target_input"}:
                raise ValueError("Column edges require source_output and target_input.")
            if edge["type"] == "dataframe" and (
                ("target_position" in edge) == ("target_input" in edge)
            ):
                raise ValueError("DataFrame edges target exactly one position or workflow input.")
            if edge["id"] in edge_ids:
                raise ValueError(f"Duplicate edge ID '{edge['id']}'.")
            edge_ids.add(edge["id"])
            if edge["source_node"] not in nodes_by_name or edge["target_node"] not in nodes_by_name:
                if partial and errors is not None:
                    errors.append(ValidationError(
                        kind="missing_input",
                        message="Edge references an unknown node.",
                        node=edge.get("target_node"),
                        edge_id=edge.get("id"),
                    ))
                    continue
                raise ValueError("Edge references an unknown node.")
            incoming[edge["target_node"]].append(edge)
            deps[edge["target_node"]].add(edge["source_node"])

        target_by_node: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for port_id, targets in serialized_targets.items():
            for target in targets:
                if not isinstance(target, dict) or set(target) != {"node", "port"}:
                    raise ValueError("Malformed workflow interface target.")
                if target["node"] not in nodes_by_name:
                    raise ValueError("Workflow interface target references an unknown node.")
                port_endpoint = target["port"]
                if not isinstance(port_endpoint, dict):
                    raise ValueError("Malformed workflow interface target port.")
                endpoint_kind = port_endpoint.get("kind")
                endpoint_fields = {
                    "field": {"kind", "name"},
                    "positional": {"kind", "index"},
                    "workflow": {"kind", "id"},
                }
                if endpoint_kind not in endpoint_fields or set(port_endpoint) != endpoint_fields[endpoint_kind]:
                    raise ValueError("Malformed workflow interface target port.")
                input_kind = wf._interface_inputs[port_id].kind
                if endpoint_kind != "workflow" and (
                    (input_kind == "field") != (endpoint_kind == "field")
                ):
                    raise ValueError("Workflow input kind does not match its target port.")
                target_by_node.setdefault(target["node"], []).append((port_id, target["port"]))

        store = _get_store_path()
        built: dict[str, Node] = {}
        previous = get_active_workflow()
        set_active_workflow(wf)
        _reset_name_counters()
        try:
            for name in TopologicalSorter(deps).static_order():
                node_data = nodes_by_name[name]
                kwargs: dict[str, Any] = {}
                positional: dict[int, Node] = {}
                positional_edge_ids: dict[int, str] = {}
                column_edge_ids: dict[str, str] = {}
                dataframe_port_edge_ids: dict[str, str] = {}
                incoming_field_names: set[str] = set()
                for edge in incoming[name]:
                    if edge["source_node"] not in built:
                        if partial and errors is not None:
                            errors.append(ValidationError(
                                kind="missing_input",
                                message=(
                                    f"Input edge source '{edge['source_node']}' "
                                    "could not be materialized."
                                ),
                                node=name,
                                edge_id=edge["id"],
                            ))
                            continue
                        raise ValueError(
                            f"Input edge source '{edge['source_node']}' could not be materialized."
                        )
                    source = built[edge["source_node"]]
                    if edge["type"] == "column":
                        kwargs[edge["target_input"]] = ColumnRef(source, edge["source_output"])
                        incoming_field_names.add(edge["target_input"])
                        column_edge_ids[edge["target_input"]] = edge["id"]
                    elif "target_position" in edge:
                        positional[edge["target_position"]] = source
                        positional_edge_ids[edge["target_position"]] = edge["id"]
                    else:
                        kwargs[edge["target_input"]] = source
                        dataframe_port_edge_ids[edge["target_input"]] = edge["id"]
                for port_id, endpoint in target_by_node.get(name, []):
                    ref = wf._input_ref(port_id)
                    if endpoint.get("kind") == "field":
                        if endpoint["name"] in kwargs:
                            raise ValueError(
                                "A workflow interface target cannot shadow an internal edge."
                            )
                        kwargs[endpoint["name"]] = ref
                    elif endpoint.get("kind") == "positional":
                        if endpoint["index"] in positional:
                            raise ValueError(
                                "A workflow interface target cannot shadow an internal edge."
                            )
                        positional[endpoint["index"]] = ref  # type: ignore[assignment]
                    elif endpoint.get("kind") == "workflow":
                        if endpoint["id"] in kwargs:
                            raise ValueError(
                                "A workflow interface target cannot shadow an internal edge."
                            )
                        kwargs[endpoint["id"]] = ref
                    else:
                        raise ValueError("Unknown workflow interface target kind.")

                try:
                    with wf.capture_errors() as captured:
                        if node_data["type"] == "workflow":
                            child_errors: list[ValidationError] = []
                            child = cls._materialize_graph(
                                node_data["workflow"],
                                custom_modules=custom_modules,
                                source_records=source_records,
                                auto_install=auto_install,
                                partial=partial,
                                errors=child_errors,
                                graph_stack=graph_stack,
                            )
                            child._build_errors = list(child_errors)
                            if errors is not None:
                                errors.extend(
                                    ValidationError(
                                        kind=error.kind,
                                        message=error.message,
                                        node=error.node,
                                        field=error.field,
                                        edge=error.edge,
                                        edge_id=error.edge_id,
                                        path=(name, *error.path),
                                    )
                                    for error in child_errors
                                )
                            child_by_id = child._interface_inputs
                            named_bindings: dict[str, Any] = {}
                            for key, value in kwargs.items():
                                port = child_by_id.get(key)
                                if port is None:
                                    raise ValueError(f"Unknown input port '{key}' on workflow node '{name}'.")
                                named_bindings[port.name] = value
                            for port_id, value in node_data.get("bindings", {}).items():
                                port = child_by_id.get(port_id)
                                if port is None:
                                    raise ValueError(f"Unknown constant input port '{port_id}'.")
                                if port_id in kwargs:
                                    raise ValueError(
                                        f"Workflow input port '{port_id}' has both an edge and a constant binding."
                                    )
                                named_bindings[port.name] = deserialize_constant(value)
                            node = child(name=name, **named_bindings)
                            node._input_column_binding_edge_ids.update(column_edge_ids)
                            node._input_dataframe_binding_edge_ids.update(dataframe_port_edge_ids)
                        else:
                            from bioimageflow.tool_loader import (
                                load_versioned_package,
                                resolve_tool_class,
                            )

                            instance = wf._resolve_tool_instance(
                                node_data,
                                store=store,
                                auto_install=auto_install,
                                load_versioned_package=load_versioned_package,
                                resolve_tool_class=resolve_tool_class,
                                custom_modules=custom_modules,
                            )
                            fallback_constants: dict[str, Any] = {}
                            for field_name, value in node_data.get("constants", {}).items():
                                decoded = deserialize_constant(value)
                                if field_name in incoming_field_names:
                                    raise ValueError(
                                        f"Tool input '{field_name}' has both an edge and a constant binding."
                                    )
                                if isinstance(kwargs.get(field_name), WorkflowInputRef):
                                    fallback_constants[field_name] = decoded
                                else:
                                    kwargs[field_name] = decoded
                            args = [positional[index] for index in range(max(positional, default=-1) + 1)]
                            if isinstance(instance, DataFrameTool):
                                node = instance(*args, name=name, output_templates=node_data.get("output_templates"), **kwargs)
                                node._arg_edge_ids = [
                                    positional_edge_ids.get(index)
                                    for index in range(len(args))
                                ]
                            else:
                                if args:
                                    raise ValueError("Processing tools cannot have positional DataFrame inputs.")
                                node = instance(name=name, output_templates=node_data.get("output_templates"), **kwargs)
                            node._constant_bindings.update(fallback_constants)
                            node._workflow_input_fallback_constants.update(
                                fallback_constants
                            )
                            node._column_binding_edge_ids.update(column_edge_ids)
                    if errors is not None:
                        errors.extend(captured)
                    node.enabled = node_data.get("enabled", True)
                    built[name] = node
                except Exception as exc:
                    if not partial:
                        raise
                    error = ValidationError(
                        kind="unknown_tool" if isinstance(exc, (ImportError, AttributeError)) else "construction_failed",
                        message=str(exc),
                        node=name,
                    )
                    if errors is not None:
                        errors.append(error)
                    wf._failed_nodes[name] = error
        finally:
            set_active_workflow(previous)

        for item in graph["interface"]["outputs"]:
            allowed = {"id", "name", "schema", "source"}
            if not isinstance(item, dict) or set(item) - allowed or not {"id", "name", "source"} <= set(item):
                raise ValueError("Malformed workflow output record.")
            if (
                not isinstance(item["id"], str)
                or not item["id"]
                or not isinstance(item["name"], str)
                or not item["name"]
            ):
                raise ValueError("Invalid workflow output ID or name.")
            source = item["source"]
            if not isinstance(source, dict) or set(source) != {"node", "column"} or source["node"] not in built:
                if partial and errors is not None:
                    errors.append(ValidationError(
                        kind="missing_input",
                        message="Workflow output references an unknown source.",
                        node=source.get("node") if isinstance(source, dict) else None,
                    ))
                    continue
                raise ValueError("Workflow output references an unknown source.")
            source_node = built[source["node"]]
            from bioimageflow.workflow_node import WorkflowNode

            if isinstance(source_node, WorkflowNode):
                if source["column"] not in source_node.workflow._interface_outputs:
                    raise ValueError("Workflow output references an unknown child output port.")
            else:
                output_schema = source_node.get_output_schema()
                if output_schema is not None and source["column"] not in output_schema:
                    raise ValueError("Workflow output references an unknown tool output column.")
            port = WorkflowOutputPort(
                id=item["id"], name=item["name"], annotation=Any,
                schema=copy.deepcopy(item.get("schema")),
                source_node=source["node"], source_output=source["column"],
            )
            if port.id in wf._interface_inputs or port.id in wf._interface_outputs or any(
                candidate.name == port.name
                for candidate in [*wf._interface_inputs.values(), *wf._interface_outputs.values()]
            ):
                raise ValueError("Duplicate workflow interface ID or name.")
            wf._interface_outputs[port.id] = port
        return wf

    def _resolve_tool_instance(
        self,
        node_data: dict[str, Any],
        *,
        store: Path,
        auto_install: bool,
        load_versioned_package: Any,
        resolve_tool_class: Any,
        custom_modules: dict[str, Any],
    ) -> Any:
        """Resolve and instantiate one executable tool from a node record."""
        pkg = node_data.get("tool_package")
        pkg_ver = node_data.get("tool_package_version")
        source_id = node_data.get("source_module")
        if source_id:
            tool_class = _resolve_custom_tool_class(
                custom_modules,
                source_id,
                node_data["tool_module"],
                node_data["tool_class"],
            )
        elif pkg and pkg_ver:
            if auto_install:
                _auto_install_if_missing(pkg, pkg_ver, store)
            load_versioned_package(pkg, pkg_ver, store)
            tool_class = resolve_tool_class(
                pkg, pkg_ver,
                node_data["tool_module"],
                node_data["tool_class"],
            )
        else:
            module = importlib.import_module(node_data["tool_module"])
            tool_class = getattr(module, node_data["tool_class"])
        return tool_class()


def _get_store_path() -> Path:
    from bioimageflow.tool_loader import _get_tool_store_path
    return _get_tool_store_path()


_LIBRARY_MODULE_PREFIXES = (
    "bioimageflow",
    "bioimageflow_core",
    "bioimageflow_common_tools",
    "bioimageflow_io_tools",
    "bioimageflow_measurement_tools",
    "bioimageflow_restoration_tools",
    "bioimageflow_sairpico_tools",
    "bioimageflow_segmentation_tools",
    "bioimageflow_spot_tools",
    "bioimageflow_tracking_tools",
)
_CUSTOM_TOOLS_PACKAGE = "tools"
_CUSTOM_TOOL_EXCLUDED_DIRS = {"__pycache__", ".pytest_cache"}
_CUSTOM_TOOL_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
_CUSTOM_TOOL_MAX_FILE_BYTES = 5 * 1024 * 1024


def _is_workflow_custom_class(cls: type) -> bool:
    """Return whether a class should be embedded in workflow exports."""
    if getattr(cls, "_bif_custom_source_hash", None):
        return True

    package = getattr(cls, "_bif_package", None)
    package_version = getattr(cls, "_bif_package_version", None)
    if package and package_version:
        return False

    module_name = getattr(cls, "__module__", "")
    if any(
        module_name == prefix or module_name.startswith(prefix + ".")
        for prefix in _LIBRARY_MODULE_PREFIXES
    ):
        return False

    try:
        source_file = inspect.getsourcefile(cls) or inspect.getfile(cls)
    except (OSError, TypeError):
        return False
    path = Path(source_file)
    return path.exists() and path.suffix == ".py"


def _find_custom_tools_dir(source_file: Path) -> Path | None:
    source_file = source_file.resolve()
    for parent in source_file.parents:
        if parent.name != _CUSTOM_TOOLS_PACKAGE:
            continue
        if (parent / "__init__.py").exists():
            return parent
    return None


@contextmanager
def _workflow_import_scope(root: Path):
    """Temporarily prefer a workflow root for imports from ``tools``."""
    root_str = str(root)
    sys.path.insert(0, root_str)
    previous_tools_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == _CUSTOM_TOOLS_PACKAGE or name.startswith(_CUSTOM_TOOLS_PACKAGE + ".")
    }
    for name in list(previous_tools_modules):
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        sys.path = [entry for entry in sys.path if entry != root_str]
        for name in [n for n in sys.modules if n == _CUSTOM_TOOLS_PACKAGE or n.startswith(_CUSTOM_TOOLS_PACKAGE + ".")]:
            sys.modules.pop(name, None)
        sys.modules.update(previous_tools_modules)


def _extract_workflow_archive(path: Path, destination: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        for member in archive.namelist():
            member_path = Path(member)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"Invalid workflow archive member: {member!r}")
        archive.extractall(destination)


def _iter_custom_tools_files(tools_dir: Path) -> Iterator[Path]:
    for path in sorted(tools_dir.rglob("*")):
        rel_parts = path.relative_to(tools_dir).parts
        if path.is_dir():
            continue
        if any(part in _CUSTOM_TOOL_EXCLUDED_DIRS for part in rel_parts):
            continue
        if any(part.startswith(".") for part in rel_parts):
            continue
        if path.suffix in _CUSTOM_TOOL_EXCLUDED_SUFFIXES:
            continue
        if path.stat().st_size > _CUSTOM_TOOL_MAX_FILE_BYTES:
            raise ValueError(
                f"Custom workflow tool file '{path}' is too large to export "
                f"({path.stat().st_size} bytes; limit "
                f"{_CUSTOM_TOOL_MAX_FILE_BYTES} bytes). Move large assets "
                "outside tools/ and load them explicitly."
            )
        yield path


def _build_custom_tools_dir_record(
    cls: type,
    source_file: Path,
    tools_dir: Path,
) -> tuple[str, dict[str, Any]]:
    files: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    for path in _iter_custom_tools_files(tools_dir):
        rel_path = Path(tools_dir.name) / path.relative_to(tools_dir)
        data = path.read_bytes()
        file_hash = hashlib.sha256(data).hexdigest()
        rel_posix = rel_path.as_posix()
        digest.update(rel_posix.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\0")
        files.append({
            "path": rel_posix,
            "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
            "source_hash": file_hash,
        })

    source_hash = digest.hexdigest()
    source_id = f"m_{source_hash[:16]}"
    return source_id, {
        "id": source_id,
        "module": cls.__module__,
        "filename": source_file.name,
        "root_package": tools_dir.name,
        "source_hash": source_hash,
        "files": files,
    }


def _get_custom_tools_dir_bundle_hash(cls: type) -> str | None:
    """Return the directory-bundle hash for a workflow-local tools/ class."""
    if not _is_workflow_custom_class(cls):
        return None
    source_file = Path(inspect.getsourcefile(cls) or inspect.getfile(cls))
    tools_dir = _find_custom_tools_dir(source_file)
    if tools_dir is None:
        return None
    _source_id, record = _build_custom_tools_dir_record(cls, source_file, tools_dir)
    return record["source_hash"]


def _workflow_custom_tools_dirs(workflow: Workflow) -> list[Path]:
    """Return workflow-local tools/ directories used by ``workflow``."""
    from bioimageflow.workflow_node import WorkflowNode

    dirs: dict[Path, None] = {}
    for node in workflow._nodes.values():
        if isinstance(node, WorkflowNode):
            for directory in _workflow_custom_tools_dirs(node.workflow):
                dirs[directory] = None
            continue

        cls = type(node.tool)
        if not _is_workflow_custom_class(cls):
            continue
        source_file = Path(inspect.getsourcefile(cls) or inspect.getfile(cls))
        tools_dir = _find_custom_tools_dir(source_file)
        if tools_dir is not None:
            dirs[tools_dir.resolve()] = None
    return list(dirs)


def _register_custom_tool_module(
    cls: type,
    *,
    records: list[dict[str, Any]],
    seen_ids: set[str],
) -> str | None:
    if not _is_workflow_custom_class(cls):
        return None

    captured_id = getattr(cls, "_bif_custom_source_id", None)
    if isinstance(captured_id, str) and captured_id in seen_ids:
        return captured_id

    source_file = Path(inspect.getsourcefile(cls) or inspect.getfile(cls))
    tools_dir = _find_custom_tools_dir(source_file)
    if tools_dir is not None:
        source_id, record = _build_custom_tools_dir_record(cls, source_file, tools_dir)
        if source_id not in seen_ids:
            records.append(record)
            seen_ids.add(source_id)
        setattr(cls, "_bif_custom_source_id", source_id)
        setattr(cls, "_bif_custom_source_hash", record["source_hash"])
        return source_id

    source = source_file.read_text(encoding="utf-8")
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    source_id = f"m_{source_hash[:16]}"
    if source_id not in seen_ids:
        records.append({
            "id": source_id,
            "module": cls.__module__,
            "filename": source_file.name,
            "source_hash": source_hash,
            "source": source,
        })
        seen_ids.add(source_id)
    setattr(cls, "_bif_custom_source_id", source_id)
    setattr(cls, "_bif_custom_source_hash", source_hash)
    return source_id


def _load_custom_sources(
    records: Iterable[dict[str, Any]],
) -> dict[str, _CustomToolBundle]:
    modules: dict[str, _CustomToolBundle] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Custom source records must be objects.")
        single_fields = {"id", "module", "filename", "source_hash", "source"}
        bundle_fields = {
            "id", "module", "filename", "root_package", "source_hash", "files",
        }
        expected_fields = bundle_fields if "files" in record else single_fields
        if set(record) != expected_fields:
            raise ValueError("Malformed custom source record.")
        source_id = record["id"]
        if not isinstance(source_id, str) or not source_id or source_id in modules:
            raise ValueError("Custom source IDs must be unique non-empty strings.")
        if "files" in record:
            if not isinstance(record["files"], list):
                raise ValueError("Custom source files must be an array.")
            for file_record in record["files"]:
                if not isinstance(file_record, dict) or set(file_record) != {
                    "path", "encoding", "content", "source_hash",
                }:
                    raise ValueError("Malformed custom source file record.")
                if file_record["encoding"] != "base64":
                    raise ValueError("Custom source file encoding must be 'base64'.")
            modules[source_id] = _load_custom_tools_dir_bundle(record)
            continue

        source = record["source"]
        expected_hash = record.get("source_hash")
        actual_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if expected_hash and expected_hash != actual_hash:
            raise ValueError(
                f"Embedded custom tool module {source_id!r} hash mismatch"
            )

        filename = record.get("filename") or f"{source_id}.py"
        module_dir = Path(tempfile.mkdtemp(prefix="bioimageflow_custom_tools_"))
        module_path = module_dir / filename
        module_path.write_text(source, encoding="utf-8")
        module_name = f"bioimageflow_custom_tools_{source_id}"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError(
                f"Cannot load embedded custom tool module {source_id!r}"
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        _stamp_embedded_custom_classes(
            module,
            source_id,
            actual_hash,
            record.get("module", ""),
        )
        modules[source_id] = _CustomToolBundle(
            source_id=source_id,
            source_hash=actual_hash,
            module=module,
        )
    return modules


def _load_custom_tools_dir_bundle(record: dict[str, Any]) -> _CustomToolBundle:
    source_id = record["id"]
    expected_hash = record.get("source_hash")
    digest = hashlib.sha256()
    root_package = record.get("root_package") or _CUSTOM_TOOLS_PACKAGE
    scoped_root = f"bioimageflow_custom_tools_{source_id}"
    temp_root = Path(tempfile.mkdtemp(prefix="bioimageflow_custom_tools_"))
    package_root = temp_root / scoped_root
    package_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")

    for file_record in record.get("files", []):
        rel_path = Path(file_record["path"])
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise ValueError(
                f"Invalid embedded custom tool path: {file_record['path']!r}"
            )
        if file_record.get("encoding") == "base64":
            data = base64.b64decode(file_record["content"])
        else:
            data = file_record["source"].encode("utf-8")
        actual_file_hash = hashlib.sha256(data).hexdigest()
        if file_record.get("source_hash") not in (None, actual_file_hash):
            raise ValueError(
                f"Embedded custom tool file {rel_path!s} hash mismatch"
            )
        rel_posix = rel_path.as_posix()
        digest.update(rel_posix.encode("utf-8"))
        digest.update(b"\0")
        digest.update(actual_file_hash.encode("ascii"))
        digest.update(b"\0")
        output_path = package_root / rel_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)

    actual_hash = digest.hexdigest()
    if expected_hash and expected_hash != actual_hash:
        raise ValueError(
            f"Embedded custom tool bundle {source_id!r} hash mismatch"
        )
    sys.path.insert(0, str(temp_root))
    bundle = _CustomToolBundle(
        source_id=source_id,
        source_hash=actual_hash,
        scoped_root=scoped_root,
        root_package=root_package,
        sys_path=str(temp_root),
    )
    return bundle


def _resolve_custom_tool_class(
    custom_modules: dict[str, _CustomToolBundle],
    source_id: str,
    module_name: str,
    class_name: str,
) -> type:
    bundle = custom_modules[source_id]
    if bundle.module is not None:
        return getattr(bundle.module, class_name)

    assert bundle.scoped_root is not None
    assert bundle.root_package is not None
    if module_name == bundle.root_package or module_name.startswith(
        bundle.root_package + "."
    ):
        scoped_module = f"{bundle.scoped_root}.{module_name}"
    else:
        scoped_module = f"{bundle.scoped_root}.{bundle.root_package}.{module_name}"
    module = importlib.import_module(scoped_module)
    _stamp_embedded_custom_package(
        bundle.scoped_root,
        bundle.source_id,
        bundle.source_hash,
        bundle.root_package,
        bundle.sys_path,
    )
    return getattr(module, class_name)


def _stamp_embedded_custom_classes(
    module: Any,
    source_id: str,
    source_hash: str,
    canonical_module: str,
) -> None:
    from bioimageflow_core.tool import BaseTool

    for attr_name in dir(module):
        try:
            obj = getattr(module, attr_name)
        except Exception:
            continue
        if not isinstance(obj, type):
            continue
        if not issubclass(obj, BaseTool):
            continue
        if obj is BaseTool:
            continue
        if getattr(obj, "__module__", "") != getattr(module, "__name__", ""):
            continue
        setattr(obj, "_bif_custom_source_hash", source_hash)
        setattr(obj, "_bif_custom_source_id", source_id)
        if canonical_module:
            setattr(obj, "_bif_canonical_module", canonical_module)


def _stamp_embedded_custom_package(
    scoped_root: str,
    source_id: str,
    source_hash: str,
    root_package: str,
    sys_path: str | None,
) -> None:
    from bioimageflow_core.tool import BaseTool

    for module_name, module in list(sys.modules.items()):
        if module is None:
            continue
        if module_name != scoped_root and not module_name.startswith(
            scoped_root + "."
        ):
            continue
        for attr_name in dir(module):
            try:
                obj = getattr(module, attr_name)
            except Exception:
                continue
            if not isinstance(obj, type):
                continue
            if not issubclass(obj, BaseTool):
                continue
            if obj is BaseTool:
                continue
            obj_module = getattr(obj, "__module__", "")
            if obj_module != scoped_root and not obj_module.startswith(
                scoped_root + "."
            ):
                continue
            canonical_module = obj.__module__
            prefix = scoped_root + "."
            if canonical_module.startswith(prefix):
                canonical_module = canonical_module[len(prefix):]
            setattr(obj, "_bif_custom_source_hash", source_hash)
            setattr(obj, "_bif_custom_source_id", source_id)
            setattr(obj, "_bif_canonical_module", canonical_module)
            if sys_path is not None:
                setattr(obj, "_bif_worker_sys_path", sys_path)
                setattr(obj, "_bif_worker_module", obj.__module__)


def _auto_install_if_missing(pkg: str, pkg_ver: str, store: Path) -> None:
    """Install a versioned package into the tool store if missing."""
    pkg_dir = store / pkg / pkg_ver / pkg
    if pkg_dir.exists():
        return
    from bioimageflow.tool_loader import ensure_installed
    # Use the module name as the PyPI name (hyphens for underscores)
    pypi_name = pkg.replace("_", "-")
    ensure_installed(pkg, pkg_ver, pypi_name, store)
