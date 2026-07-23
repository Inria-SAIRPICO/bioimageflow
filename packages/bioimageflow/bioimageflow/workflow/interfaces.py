"""Focused methods extracted from the workflow façade."""

# Pyright checks the complete contract on Workflow; this module contains one partial mixin.
# pyright: reportArgumentType=false, reportReturnType=false

from __future__ import annotations

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
    _reset_name_counters,
    copy,
    get_active_workflow,
    set_active_workflow,
    threading,
)

if TYPE_CHECKING:
    from .model import Workflow


class _InterfacesMixin:
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
        self._storage_path_config = str(storage_path)
        self.storage_path = _absolute_runtime_path(storage_path)
        self.engine_type = engine
        self.execution = execution
        self.on_progress = on_progress
        self.wetlands_config = wetlands_config
        self.max_workers = max_workers
        self.output_view = _normalize_output_view(output_view)
        self._env_configs: dict[str, WorkflowEnvironment] = {}
        self._execution_lock = threading.RLock()
        self._active_run_context: Any = None
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
        return next(
            (port for port in self._interface_inputs.values() if port.name == name),
            None,
        )

    def _output_by_name(self, name: str) -> WorkflowOutputPort | None:
        return next(
            (port for port in self._interface_outputs.values() if port.name == name),
            None,
        )

    def output(self, name: str, source: Any, *, id: str | None = None) -> None:
        """Publish an internal node column as a workflow output."""
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

        annotation: Any = Any
        schema: dict[str, Any] | None = None
        if isinstance(source.node, WorkflowNode):
            child_port = source.node.workflow._interface_outputs.get(source.column)
            if child_port is None:
                raise ValueError(
                    f"Unknown child workflow output port '{source.column}'."
                )
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
            raise ValueError(
                "A symbolic workflow input may only be bound in its owning active workflow."
            )
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
        snapshot = type(self)(
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
        snapshot._captured_custom_sources = copy.deepcopy(
            self._captured_custom_sources, memo
        )
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
