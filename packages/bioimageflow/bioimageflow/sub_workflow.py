"""SubWorkflow — use an entire workflow DAG as a reusable node."""

from __future__ import annotations

import warnings
from typing import Any

from bioimageflow_core.tool import IOModel

from bioimageflow.node import (
    Node,
    ColumnRef,
    BindingError,
    _get_next_name,
    get_active_workflow,
    set_active_workflow,
)


class _ProxyTool:
    """Fake tool attached to the proxy node so Node.__getitem__ works.

    The proxy node acts as a source node whose Outputs match the
    SubWorkflow's Inputs (the proxy *produces* whatever the sub-workflow
    *consumes*).
    """

    def __init__(self, outputs_cls: type[IOModel] | None, name: str) -> None:
        self.Outputs = outputs_cls
        self.name = name
        self.Inputs = IOModel  # no real inputs


class SubWorkflowInputProxy:
    """Proxy passed to ``SubWorkflow.build()``.

    Provides ``proxy.field`` and ``proxy["field"]`` access.  For
    column-bound inputs the proxy returns a :class:`ColumnRef` pointing
    to the hidden proxy node.  For constant or default inputs it returns
    the raw Python value, so the internal node stores it as a constant
    binding (avoiding numpy-coercion issues when constants travel through
    a pandas DataFrame).
    """

    def __init__(
        self,
        inputs_cls: type[IOModel],
        proxy_node: Node,
        column_bound_fields: set[str],
        constant_values: dict[str, Any],
    ) -> None:
        self._inputs_cls = inputs_cls
        self._proxy_node = proxy_node
        self._annotations = inputs_cls._get_all_annotations()
        self._column_bound_fields = column_bound_fields
        self._constant_values = constant_values

    def _resolve(self, name: str) -> Any:
        """Return a ColumnRef for column-bound fields, or the constant value."""
        if name in self._column_bound_fields:
            return ColumnRef(node=self._proxy_node, column=name)
        if name in self._constant_values:
            return self._constant_values[name]
        # Default from Inputs class
        if hasattr(self._inputs_cls, name):
            return getattr(self._inputs_cls, name)
        raise AttributeError(
            f"SubWorkflow.Inputs field '{name}' has no column binding, "
            f"constant, or default."
        )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._annotations:
            raise AttributeError(
                f"SubWorkflow.Inputs has no field '{name}'. "
                f"Available: {list(self._annotations)}"
            )
        return self._resolve(name)

    def __getitem__(self, name: str) -> Any:
        if name not in self._annotations:
            raise KeyError(
                f"SubWorkflow.Inputs has no field '{name}'. "
                f"Available: {list(self._annotations)}"
            )
        return self._resolve(name)


class SubWorkflowNode(Node):
    """A node representing a flattened sub-workflow in the parent DAG.

    Attributes:
        sub_workflow: The SubWorkflow definition.
        internal_nodes: List of internal Node objects (for debugging).
        _proxy_node: The proxy source node (replaced at execution time).
        _output_mapping: Maps Outputs field names → ColumnRef from internal nodes.
        _input_column_bindings: Maps Inputs field names → parent ColumnRef.
        _input_constant_bindings: Maps Inputs field names → constant values.
    """

    def __init__(
        self,
        sub_workflow: SubWorkflow,
        internal_nodes: list[Node],
        proxy_node: Node,
        output_mapping: dict[str, ColumnRef],
        input_column_bindings: dict[str, ColumnRef],
        input_constant_bindings: dict[str, Any],
        name: str | None = None,
    ) -> None:
        # Bypass Node.__init__ — we manage our own state
        self.tool = _ProxyTool(sub_workflow.Outputs, sub_workflow.name)  # type: ignore[assignment]
        self.sub_workflow = sub_workflow
        self.internal_nodes = internal_nodes
        self._proxy_node = proxy_node
        self._output_mapping = output_mapping
        self._input_column_bindings = input_column_bindings
        self._input_constant_bindings = input_constant_bindings
        self.enabled = True
        self._args: list[Any] = []
        self._kwargs: dict[str, Any] = {}
        self._column_bindings: dict[str, ColumnRef] = {}
        self._constant_bindings: dict[str, Any] = {}

        # Upstream nodes: everything referenced via input column bindings
        self._upstream_nodes: set[Node] = set()
        for col_ref in input_column_bindings.values():
            self._upstream_nodes.add(col_ref.node)

        # Name
        if name is not None:
            self._name = name
        else:
            self._name = _get_next_name(sub_workflow.name)

        # Register with active workflow
        wf = get_active_workflow()
        if wf is not None:
            if name is not None and name in wf._nodes:
                raise ValueError(
                    f"Node name '{name}' is not unique. Each node in a Workflow "
                    f"must have a unique name."
                )
            wf._register_node(self)

    def __getitem__(self, column: str) -> ColumnRef:
        """Access an output column of the sub-workflow."""
        if self.sub_workflow.Outputs is not None:
            output_annotations = self.sub_workflow.Outputs._get_all_annotations()
            if column not in output_annotations:
                from bioimageflow.node import ColumnNotFoundError
                available = list(output_annotations.keys())
                raise ColumnNotFoundError(
                    f"Column '{column}' not found in SubWorkflow "
                    f"'{self.sub_workflow.name}' outputs. "
                    f"Available: {available}"
                )
        return ColumnRef(node=self, column=column)


class SubWorkflow:
    """Base class for reusable sub-workflow definitions.

    Subclasses must declare:
    - ``name``: str — unique identifier
    - ``Inputs``: IOModel subclass — declared inputs
    - ``Outputs``: IOModel subclass — declared outputs
    - ``build(self, inputs)``: method returning a dict mapping output names
      to ColumnRefs from internal nodes
    """

    name: str
    Inputs: type[IOModel] = IOModel
    Outputs: type[IOModel] | None = None

    def build(self, inputs: SubWorkflowInputProxy) -> dict[str, ColumnRef]:
        """Build the internal DAG. Override in subclasses."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement build()."
        )

    def __call__(self, *, name: str | None = None, **kwargs: Any) -> SubWorkflowNode:
        """Create a SubWorkflowNode by building the internal DAG.

        Keyword arguments are validated against Inputs (same rules as
        ProcessingTool: ColumnRef, Node shorthand, or constants).
        """
        input_annotations = self.Inputs._get_all_annotations()

        # Separate column bindings from constants
        input_column_bindings: dict[str, ColumnRef] = {}
        input_constant_bindings: dict[str, Any] = {}

        for key, value in kwargs.items():
            if key == "name":
                continue
            if key not in input_annotations:
                raise BindingError(
                    f"Unknown input '{key}' for SubWorkflow '{self.name}'. "
                    f"Available: {list(input_annotations)}"
                )
            if isinstance(value, ColumnRef):
                input_column_bindings[key] = value
            elif isinstance(value, Node):
                # Node shorthand: field=node -> field=node["field"]
                input_column_bindings[key] = value[key]
            else:
                input_constant_bindings[key] = value

        # Check required fields
        for field_name in input_annotations:
            if field_name in input_column_bindings:
                continue
            if field_name in input_constant_bindings:
                continue
            if hasattr(self.Inputs, field_name):
                continue  # has default
            raise BindingError(
                f"Missing required input '{field_name}' for SubWorkflow "
                f"'{self.name}'. No column reference, constant, or default."
            )

        # Create proxy node — temporarily suppress parent workflow registration
        parent_wf = get_active_workflow()
        set_active_workflow(None)

        try:
            # Create proxy node with Inputs as its "Outputs" so internal
            # nodes can reference proxy["field"]
            proxy_tool = _ProxyTool(self.Inputs, f"_proxy_{self.name}")
            proxy_node = Node.__new__(Node)
            proxy_node.tool = proxy_tool  # type: ignore[assignment]
            proxy_node._name = f"_proxy_{self.name}"
            proxy_node._kwargs = {}
            proxy_node._args = []
            proxy_node.enabled = True
            proxy_node._upstream_nodes = set()
            proxy_node._column_bindings = {}
            proxy_node._constant_bindings = {}

            proxy = SubWorkflowInputProxy(
                self.Inputs,
                proxy_node,
                column_bound_fields=set(input_column_bindings),
                constant_values=input_constant_bindings,
            )

            # Build internal DAG — nodes created here are NOT registered
            # with the parent workflow
            output_mapping = self.build(proxy)
        finally:
            set_active_workflow(parent_wf)

        # Validate output mapping
        if self.Outputs is not None:
            output_annotations = self.Outputs._get_all_annotations()
            missing = set(output_annotations) - set(output_mapping or {})
            if missing:
                raise ValueError(
                    f"SubWorkflow '{self.name}' build() did not return mappings "
                    f"for all Outputs fields. Missing: {sorted(missing)}"
                )
            extra = set(output_mapping or {}) - set(output_annotations)
            if extra:
                warnings.warn(
                    f"SubWorkflow '{self.name}' build() returned extra output "
                    f"keys not in Outputs: {sorted(extra)}. They will be ignored.",
                    stacklevel=2,
                )

        # Collect all internal nodes by traversing from output mapping
        internal_nodes = _collect_internal_nodes(output_mapping, proxy_node)

        return SubWorkflowNode(
            sub_workflow=self,
            internal_nodes=internal_nodes,
            proxy_node=proxy_node,
            output_mapping=output_mapping,
            input_column_bindings=input_column_bindings,
            input_constant_bindings=input_constant_bindings,
            name=name,
        )


def _collect_internal_nodes(
    output_mapping: dict[str, ColumnRef],
    proxy_node: Node,
) -> list[Node]:
    """Collect all internal nodes reachable from the output mapping."""
    visited: set[str] = set()
    result: list[Node] = []
    queue: list[Node] = []

    for col_ref in output_mapping.values():
        if col_ref.node is not proxy_node and col_ref.node.name not in visited:
            queue.append(col_ref.node)

    while queue:
        node = queue.pop(0)
        if node.name in visited or node is proxy_node:
            continue
        visited.add(node.name)
        result.append(node)
        for up in node._upstream_nodes:
            if up is not proxy_node and up.name not in visited:
                queue.append(up)
        for arg in node._args:
            if isinstance(arg, Node) and arg is not proxy_node and arg.name not in visited:
                queue.append(arg)

    return result
