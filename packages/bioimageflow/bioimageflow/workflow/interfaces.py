"""Focused methods extracted from the workflow façade."""

# Pyright checks the complete contract on Workflow; this module contains one partial mixin.
# pyright: reportArgumentType=false, reportReturnType=false

from __future__ import annotations

import contextvars
from bioimageflow.node import BindingError
from typing import Any, Callable, Literal, Mapping, TYPE_CHECKING

from .common import (
    MISSING,
    Node,
    OutputView,
    Path,
    ProgressEvent,
    ValidationError,
    WorkflowEnvironment,
    WorkflowInputPort,
    WorkflowInputRef,
    WorkflowOutputPort,
    _absolute_runtime_path,
    _annotation_schema,
    _new_port_id,
    _normalize_output_view,
    copy,
    get_active_workflow,
    set_active_workflow,
    threading,
)

if TYPE_CHECKING:
    from .model import Workflow


_entry_tokens: contextvars.ContextVar[tuple[Any, ...]] = contextvars.ContextVar("_workflow_entry_tokens", default=())


def _resolve_output_source(
    node: Node, column: str,
) -> tuple[Any, dict[str, Any] | None] | None:
    """Capture source semantics, distinguishing an unknown port from dynamic IO."""
    from bioimageflow.workflow_node import WorkflowNode

    if isinstance(node, WorkflowNode):
        port = node.workflow._interface_outputs.get(column)
        return None if port is None else (port.annotation, copy.deepcopy(port.schema))
    resolved = node.get_resolved_output_schema()
    semantic = resolved.get(column)
    if semantic is not None:
        return semantic.annotation, semantic.to_wire()
    return (Any, None) if resolved.state == "dynamic" else None


class _InterfacesMixin:
    def __init__(
        self,
        storage_path: str | Path | None = None,
        *,
        name: str = "workflow",
        display_name: str | None = None,
        engine: str = "wetlands",
        execution: str = "parallel",
        on_progress: Callable[[ProgressEvent], None] | None = None,
        wetlands_config: dict[str, Any] | None = None,
        max_workers: int = 1,
        output_view: OutputView | Mapping[str, Any] | str | None = None,
        shared_memory_context: Any = None,
    ) -> None:
        if not name or "/" in name:
            raise ValueError("Workflow name must be non-empty and may not contain '/'.")
        if engine not in {"direct", "wetlands", "parsl"}:
            raise ValueError(
                f"Unknown engine '{engine}'. Expected 'direct', 'wetlands', or 'parsl'."
            )
        if execution not in {"parallel", "sequential"}:
            raise ValueError(
                f"Unknown execution '{execution}'. Expected 'parallel' or 'sequential'."
            )
        self.name = name
        self.display_name = display_name if display_name is not None else name
        self.storage_path = (
            None if storage_path is None else _absolute_runtime_path(storage_path)
        )
        self.engine_type = engine
        self.execution = execution
        self.on_progress = on_progress
        self.wetlands_config = wetlands_config
        self.max_workers = max_workers
        self.output_view = _normalize_output_view(output_view)
        self._env_configs: dict[str, WorkflowEnvironment] = {}
        if shared_memory_context is not None:
            from bioimageflow_core import SharedMemoryContext
            if not isinstance(shared_memory_context, SharedMemoryContext):
                raise TypeError("shared_memory_context must be a SharedMemoryContext")
        self._shared_memory_context = shared_memory_context
        self._execution_lock = threading.RLock()
        self._active_run_context: Any = None
        self._nodes: dict[str, Node] = {}
        self._prev_workflow: Any = None
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
        self._imported_viewing_requirements: Any = None
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
        port = self._plan_input_port(name, annotation, kind=kind, default=default, id=id)
        self._interface_inputs[port.id] = port
        return self._input_ref(port.id)

    def _plan_input_port(self, name: str, annotation: Any, *, kind: Literal["field", "dataframe"], default: Any, id: str | None) -> WorkflowInputPort:
        if name == "name":
            raise ValueError(
                "'name' is reserved for a workflow invocation's node name."
            )
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
        return port

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
        port = self._plan_input_port(name, annotation, kind=kind, default=default, id=id)
        ref = WorkflowInputRef(self, port.id, port.name, port.kind, port.annotation)
        record = self._plan_input_target(ref, node, target, kind=kind, port=port)
        self._interface_inputs[port.id] = port
        port.targets.append(record)
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
        node._refresh_dependencies()
        return ref

    def _input_by_name(self, name: str) -> WorkflowInputPort | None:
        return next(
            (port for port in self._interface_inputs.values() if port.name == name),
            None,
        )

    def _output_by_name(self, name: str) -> WorkflowOutputPort | None:
        return next(
            (port for port in self._interface_outputs.values() if port.name == name),
            None,
        )

    def output(
        self,
        name: str,
        source: Any,
        *,
        id: str | None = None,
        viewer_addition: Any | None = None,
    ) -> None:
        """Publish an internal node column as a workflow output."""
        from bioimageflow_core.viewer import coerce_viewer_spec
        from bioimageflow.node import ColumnRef
        from bioimageflow.workflow_node import WorkflowNode

        if not isinstance(source, ColumnRef):
            raise TypeError("Workflow.output source must be a ColumnRef.")
        if (
            source.node.name not in self._nodes
            or self._nodes[source.node.name] is not source.node
        ):
            raise ValueError(
                "Workflow output sources must belong to the same workflow."
            )
        if any(port.name == name for port in self._interface_inputs.values()) or any(
            port.name == name for port in self._interface_outputs.values()
        ):
            raise ValueError(f"Workflow interface name '{name}' is not unique.")
        port_id = id or _new_port_id("output")
        if port_id in self._interface_inputs or port_id in self._interface_outputs:
            raise ValueError(f"Workflow interface ID '{port_id}' is not unique.")

        declaration = _resolve_output_source(source.node, source.column)
        if declaration is None:
            if isinstance(source.node, WorkflowNode):
                raise ValueError(
                    f"Unknown child workflow output port '{source.column}'."
                )
            raise ValueError(f"Column '{source.column}' is not a resolved output of node '{source.node.name}'.")
        annotation, schema = declaration
        self._interface_outputs[port_id] = WorkflowOutputPort(
            id=port_id,
            name=name,
            annotation=annotation,
            schema=schema,
            source_node=source.node.name,
            source_output=source.column,
            viewer_addition=(
                None
                if viewer_addition is None
                else coerce_viewer_spec(viewer_addition)
            ),
        )

    def get_output_viewer_spec(self, output: str) -> Any | None:
        """Resolve inherited and boundary-added viewer metadata for an output."""
        from bioimageflow_core.viewer import merge_viewer_specs

        port = self._interface_outputs.get(output) or self._output_by_name(output)
        if port is None:
            raise KeyError(f"Unknown workflow output {output!r}.")
        source = self._nodes.get(port.source_node)
        if source is None:
            return port.viewer_addition
        return merge_viewer_specs(
            source.get_output_viewer_spec(port.source_output),
            port.viewer_addition,
        )

    def set_output_viewer_addition(
        self,
        output: str,
        value: Any | None,
    ) -> "Workflow":
        """Set or clear an additive declaration on one public output."""
        from bioimageflow_core.viewer import coerce_viewer_spec

        port = self._interface_outputs.get(output) or self._output_by_name(output)
        if port is None:
            raise KeyError(f"Unknown workflow output {output!r}.")
        port.viewer_addition = None if value is None else coerce_viewer_spec(value)
        return self

    def _plan_input_target(
        self, ref: WorkflowInputRef, node: Node, target: str | int, *,
        kind: Literal["field", "dataframe"], port: WorkflowInputPort | None = None,
    ) -> dict[str, Any]:
        if ref.workflow is not self or get_active_workflow() is not self:
            raise ValueError("A symbolic workflow input may only be bound in its owning active workflow.")
        port = port if port is not None else self._interface_inputs.get(ref.port_id)
        if port is None or port.kind != kind:
            raise ValueError(f"Workflow input '{ref.name}' cannot target a {kind} input.")
        from bioimageflow.workflow_node import WorkflowNode
        if isinstance(node, WorkflowNode):
            child = node.workflow._interface_inputs.get(str(target))
            if child is None or child.kind != kind:
                raise ValueError("Unknown or incompatible child workflow input target.")
            descriptor = {"kind": "workflow", "id": str(target)}
        elif kind == "field":
            if target not in node.tool.Inputs._get_all_annotations():
                raise ValueError(f"Unknown input target '{target}'.")
            if target in node._column_bindings:
                raise ValueError(f"Node '{node.name}' input '{target}' already has an internal data edge.")
            descriptor = {"kind": "field", "name": str(target)}
        else:
            from bioimageflow.dataframe_tool import DataFrameTool
            if not isinstance(node.tool, DataFrameTool) or not isinstance(target, int) or isinstance(target, bool) or target < 0:
                raise ValueError("Positional targets require a DataFrameTool and a nonnegative integer position.")
            if target < len(node._args) and isinstance(node._args[target], Node):
                raise ValueError(f"Node '{node.name}' positional input {target} already has an internal data edge.")
            descriptor = {"kind": "positional", "index": target}
        record = {"node": node.name, "port": descriptor}
        for other in self._interface_inputs.values():
            if other.id != port.id and record in other.targets:
                raise ValueError(f"Internal target {node.name}:{target} is already published by '{other.name}'.")
        return record

    def _bind_input_target(self, ref: WorkflowInputRef, node: Node, target: str | int, *, kind: Literal["field", "dataframe"]) -> None:
        record = self._plan_input_target(ref, node, target, kind=kind)
        port = self._interface_inputs[ref.port_id]
        pending = getattr(node, "_pending_interface_targets", None)
        if self._nodes.get(node.name) is not node and pending is not None:
            if (port, record) not in pending:
                pending.append((port, record))
        elif record not in port.targets:
            port.targets.append(record)

    def _check_interface_binding(self, port_id: str, value: Any) -> None:
        from bioimageflow.node import ColumnRef, Node
        from bioimageflow.workflow_node import WorkflowNode
        import pandas as pd
        for target in self._interface_inputs[port_id].targets:
            node = self._nodes[target["node"]]
            endpoint = target["port"]
            if isinstance(node, WorkflowNode):
                node.workflow._check_interface_binding(endpoint["id"], value)
            elif endpoint["kind"] == "field":
                if isinstance(value, ColumnRef):
                    node._check_column_binding_allowed(endpoint["name"])
                    node._check_type_compat(endpoint["name"], value)
            elif not isinstance(value, (Node, pd.DataFrame)):
                raise TypeError(f"DataFrame workflow input '{self._interface_inputs[port_id].name}' requires a complete DataFrame or upstream node.")

    def _check_interface_column_binding(self, port_id: str) -> None:
        """Check every nested target before mutating a fanned-out field binding."""
        from bioimageflow.workflow_node import WorkflowNode

        for target in self._interface_inputs[port_id].targets:
            node = self._nodes[target["node"]]
            endpoint = target["port"]
            if isinstance(node, WorkflowNode):
                node.workflow._check_interface_column_binding(endpoint["id"])
            elif endpoint["kind"] == "field":
                node._check_column_binding_allowed(endpoint["name"])

    def _apply_interface_binding(self, port_id: str, value: Any) -> None:
        """Substitute one boundary value at every target in this definition."""
        from bioimageflow.node import ColumnRef, Node
        from bioimageflow.workflow_node import WorkflowNode

        self._check_interface_binding(port_id, value)
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
                    node._check_column_binding_allowed(field_name)
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
            node._refresh_dependencies()

    def _snapshot_definition(
        self,
        memo: dict[int, Any] | None = None,
        *,
        storage_path: str | Path | None = None,
    ) -> "Workflow":
        """Copy definition state without copying live execution state."""
        runtime_storage = (
            self.storage_path
            if storage_path is None
            else _absolute_runtime_path(storage_path)
        )
        snapshot = type(self)(
            name=self.name,
            display_name=self.display_name,
            storage_path=runtime_storage,
            engine=self.engine_type,
            execution=self.execution,
            wetlands_config=copy.deepcopy(self.wetlands_config),
            max_workers=self.max_workers,
            output_view=copy.deepcopy(self.output_view),
        )
        memo = memo if memo is not None else {}
        memo[id(self)] = snapshot
        snapshot._nodes = copy.deepcopy(self._nodes, memo)
        snapshot._env_configs = copy.deepcopy(self._env_configs, memo)
        snapshot._build_errors = copy.deepcopy(self._build_errors, memo)
        snapshot._failed_nodes = copy.deepcopy(self._failed_nodes, memo)
        snapshot._expected_node_names = copy.deepcopy(self._expected_node_names, memo)
        snapshot._accept_root_dataframes = self._accept_root_dataframes
        snapshot._interface_inputs = copy.deepcopy(self._interface_inputs, memo)
        snapshot._interface_outputs = copy.deepcopy(self._interface_outputs, memo)
        snapshot._captured_custom_sources = copy.deepcopy(
            self._captured_custom_sources, memo
        )
        snapshot._imported_viewing_requirements = copy.deepcopy(
            self._imported_viewing_requirements,
            memo,
        )
        snapshot._inherit_runtime_storage(runtime_storage)
        return snapshot

    def _capture_definition(self, targets: Any = None) -> tuple["Workflow", tuple[Node, ...] | None]:
        """Admit one owned effective definition and remap explicit target nodes."""
        if self._build_errors or self._failed_nodes or self.is_partial:
            raise BindingError("Cannot execute a diagnostic workflow with unresolved construction errors.")
        memo: dict[int, Any] = {}
        snapshot = self._snapshot_definition(memo)
        if targets is None:
            return snapshot, None
        original = tuple(targets)
        mapped: list[Node] = []
        for node in original:
            if not isinstance(node, Node):
                raise TypeError("Execution targets must be Nodes.")
            captured = copy.deepcopy(node, memo)
            if captured.name in snapshot._nodes and snapshot._nodes[captured.name] is not captured:
                raise ValueError(f"Execution target name '{captured.name}' conflicts with the captured workflow.")
            snapshot._nodes[captured.name] = captured
            mapped.append(captured)
        return snapshot, tuple(mapped)

    def _inherit_runtime_storage(
        self,
        storage_path: str | Path | None,
        seen: set[int] | None = None,
    ) -> None:
        """Apply one root runtime storage path through nested snapshots."""
        from bioimageflow.workflow_node import WorkflowNode

        seen = seen if seen is not None else set()
        if id(self) in seen:
            return
        seen.add(id(self))
        self.storage_path = (
            None if storage_path is None else _absolute_runtime_path(storage_path)
        )
        for node in self._nodes.values():
            if isinstance(node, WorkflowNode):
                node.workflow._inherit_runtime_storage(self.storage_path, seen)

    def __call__(
        self,
        *,
        name: str | None = None,
        viewer_additions: dict[str, Any] | None = None,
        **bindings: Any,
    ) -> Any:
        """Capture this definition as a WorkflowNode in the active parent."""
        from bioimageflow.workflow_node import WorkflowNode

        by_name = {port.name: port for port in self._interface_inputs.values()}
        unknown = set(bindings) - set(by_name)
        if unknown:
            raise ValueError(
                f"Unknown workflow input(s) for '{self.name}': {sorted(unknown)}."
            )
        stable_bindings = {by_name[key].id: value for key, value in bindings.items()}
        parent = get_active_workflow()
        runtime_storage = self.storage_path if parent is None else parent.storage_path
        return WorkflowNode(
            self._snapshot_definition(storage_path=runtime_storage),
            name=name,
            bindings=stable_bindings,
            viewer_additions=viewer_additions,
        )

    def __enter__(self) -> "Workflow":
        token = set_active_workflow(self)
        _entry_tokens.set((*_entry_tokens.get(), (self, token)))
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Literal[False]:
        from bioimageflow.node import reset_active_workflow
        entries = _entry_tokens.get()
        owner, token = entries[-1]
        if owner is not self:
            raise RuntimeError("Workflow contexts must exit in nesting order.")
        reset_active_workflow(token)
        _entry_tokens.set(entries[:-1])
        return False

    def _register_node(self, node: Node) -> None:
        """Register a node with this workflow."""
        if node.name in self._nodes:
            raise ValueError(f"Node name '{node.name}' is not unique.")
        pending = node._pending_interface_targets
        for port, record in pending:
            if self._interface_inputs.get(port.id) is not port:
                raise ValueError("Workflow input changed before node admission.")
            if any(record in other.targets for other in self._interface_inputs.values() if other.id != port.id):
                raise ValueError("A target is already published by another workflow input.")
        self._nodes[node.name] = node
        self._build_errors.extend(getattr(node, "_construction_errors", ()))
        for port, record in pending:
            if record not in port.targets:
                port.targets.append(record)
        pending.clear()

    @property
    def nodes(self) -> dict[str, Node]:
        return dict(self._nodes)

    @property
    def imported_viewing_requirements(self) -> Any | None:
        """Return the archive snapshot retained at import, if one was present."""
        return self._imported_viewing_requirements

    def viewing_requirements(self) -> Any:
        """Derive current requirements from authoritative loaded metadata."""
        from bioimageflow.viewing import derive_viewing_requirements

        return derive_viewing_requirements(self)

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
