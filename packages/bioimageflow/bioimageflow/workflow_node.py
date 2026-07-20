"""A workflow invocation embedded as a node in another workflow."""

from __future__ import annotations

import copy
from typing import Any, TYPE_CHECKING

from bioimageflow.node import (
    BindingError,
    ColumnNotFoundError,
    ColumnRef,
    Node,
    _get_next_name,
    get_active_workflow,
)
from bioimageflow_core.tool import IOModel

if TYPE_CHECKING:
    from bioimageflow.workflow import Workflow


def _interface_model(name: str, fields: dict[str, Any]) -> type[IOModel]:
    """Build the lightweight IOModel facade expected by graph validation."""
    namespace: dict[str, Any] = {"__annotations__": fields}
    return type(name, (IOModel,), namespace)


class _WorkflowToolFacade:
    """Expose a workflow interface through the small Node.tool protocol."""

    def __init__(self, workflow: "Workflow") -> None:
        self.name = workflow.name
        self.Inputs = _interface_model(
            f"{workflow.name}Inputs",
            {
                port.id: port.annotation
                for port in workflow._interface_inputs.values()
                if port.kind == "field"
            },
        )
        self.Outputs = _interface_model(
            f"{workflow.name}Outputs",
            {port.id: port.annotation for port in workflow._interface_outputs.values()},
        )


class WorkflowNode(Node):
    """An independent structural snapshot of a workflow invocation."""

    def __init__(
        self,
        workflow: "Workflow",
        *,
        name: str | None = None,
        bindings: dict[str, Any] | None = None,
    ) -> None:
        self.workflow = workflow
        self.tool = _WorkflowToolFacade(workflow)  # type: ignore[assignment]
        self.enabled = True
        self.output_templates: dict[str, str] = {}
        self._args: list[Any] = []
        self._kwargs: dict[str, Any] = {}
        self._column_bindings: dict[str, ColumnRef] = {}
        self._constant_bindings: dict[str, Any] = {}
        self._column_binding_edge_ids: dict[str, str | None] = {}
        self._arg_edge_ids: list[str | None] = []
        self._upstream_nodes: set[Node] = set()
        self._input_column_bindings: dict[str, ColumnRef] = {}
        self._input_dataframe_bindings: dict[str, Any] = {}
        self._input_constant_bindings: dict[str, Any] = {}
        self._workflow_input_bindings: dict[str, Any] = {}
        self._is_root_boundary = False
        self._input_column_binding_edge_ids: dict[str, str | None] = {}
        self._input_dataframe_binding_edge_ids: dict[str, str | None] = {}
        self._name = name if name is not None else _get_next_name(workflow.name)
        if not self._name or "/" in self._name:
            raise ValueError(
                "WorkflowNode names must be non-empty and may not contain '/'."
            )

        parent = get_active_workflow()
        if parent is None or parent is workflow:
            raise RuntimeError(
                "Calling a Workflow requires an active, distinct parent Workflow; "
                "use compute(inputs=...) for root execution."
            )
        if self._name in parent._nodes:
            raise ValueError(f"Node name '{self._name}' is not unique in the parent Workflow.")

        for port_id, value in (bindings or {}).items():
            self.bind_port(port_id, value)

        missing = [
            port.name
            for port in workflow._interface_inputs.values()
            if port.id not in self._bound_port_ids() and not port.has_fallback(workflow)
        ]
        if missing:
            raise BindingError(
                f"Missing required workflow input(s) for '{workflow.name}': {missing}."
            )
        for port in workflow._interface_inputs.values():
            if port.id not in self._bound_port_ids() and port.default is not workflow.MISSING:
                self.bind_port(port.id, port.default)

        parent._register_node(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> "WorkflowNode":
        existing = memo.get(id(self))
        if existing is not None:
            return existing
        clone = object.__new__(WorkflowNode)
        memo[id(self)] = clone
        clone.workflow = self.workflow._snapshot_definition(memo)
        clone.tool = _WorkflowToolFacade(clone.workflow)  # type: ignore[assignment]
        for key, value in self.__dict__.items():
            if key in {"workflow", "tool"}:
                continue
            setattr(clone, key, copy.deepcopy(value, memo))
        return clone

    @property
    def internal_nodes(self) -> list[Node]:
        return list(self.workflow._nodes.values())

    @property
    def _published_outputs(self) -> dict[str, ColumnRef]:
        return {
            port.name: ColumnRef(
                self.workflow._nodes[port.source_node],
                port.source_output,
            )
            for port in self.workflow._interface_outputs.values()
        }

    def _bound_port_ids(self) -> set[str]:
        return (
            set(self._input_column_bindings)
            | set(self._input_dataframe_bindings)
            | set(self._input_constant_bindings)
            | set(self._workflow_input_bindings)
        )

    def bind_port(self, port_id: str, value: Any) -> None:
        """Bind one stable input port and apply it to every declared target."""
        port = self.workflow._interface_inputs.get(port_id)
        if port is None:
            raise BindingError(
                f"Unknown workflow input port '{port_id}' for '{self.workflow.name}'."
            )
        from bioimageflow.workflow import WorkflowInputRef

        if isinstance(value, WorkflowInputRef):
            parent = get_active_workflow()
            if parent is None or value.workflow is not parent:
                raise BindingError("A symbolic workflow input may only be used in its owning workflow.")
            if value.kind != port.kind:
                raise BindingError(
                    f"Workflow input kind mismatch: '{value.name}' is {value.kind}, "
                    f"but '{port.name}' is {port.kind}."
                )
            value.workflow._bind_input_target(
                value,
                self,
                port.id,
                kind=port.kind,
            )
            self._workflow_input_bindings[port_id] = value
            return

        if port.kind == "field":
            if isinstance(value, Node) and not isinstance(value, ColumnRef):
                value = value[port.name]
            if isinstance(value, ColumnRef):
                self._input_column_bindings[port_id] = value
                self._upstream_nodes.add(value.node)
            else:
                self._input_constant_bindings[port_id] = value
        else:
            import pandas as pd

            is_root_dataframe = isinstance(value, pd.DataFrame) and bool(
                getattr(get_active_workflow(), "_accept_root_dataframes", False)
            )
            if not isinstance(value, Node) and not is_root_dataframe:
                raise BindingError(
                    f"DataFrame workflow input '{port.name}' requires an upstream Node."
                )
            self._input_dataframe_bindings[port_id] = value
            if isinstance(value, Node):
                self._upstream_nodes.add(value)

        self.workflow._apply_interface_binding(port_id, value)

    def output_name_for_id(self, port_id: str) -> str:
        port = self.workflow._interface_outputs.get(port_id)
        if port is None:
            raise ColumnNotFoundError(
                f"Workflow '{self.workflow.name}' has no output port '{port_id}'."
            )
        return port.name

    def __getitem__(self, name: str) -> ColumnRef:
        port = self.workflow._output_by_name(name)
        if port is None:
            raise ColumnNotFoundError(
                f"Output '{name}' not found in Workflow '{self.workflow.name}'. "
                f"Available: {[p.name for p in self.workflow._interface_outputs.values()]}"
            )
        return ColumnRef(node=self, column=port.id)
