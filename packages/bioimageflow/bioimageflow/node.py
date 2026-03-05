"""Node and ColumnRef — graph construction primitives."""

import threading
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any

from bioimageflow_core.tool import ProcessingTool, BaseTool
from bioimageflow.validation import extract_image_spec, is_path_type
from bioimageflow_core.types import check_compatibility


class ColumnNotFoundError(Exception):
    """Raised when a column reference targets a non-existent column."""
    pass


class BindingError(Exception):
    """Raised when a required input field has no source."""
    pass


class IndexAlignmentError(Exception):
    """Raised when upstream indices are incompatible."""
    pass


@dataclass(frozen=True)
class ColumnRef:
    """References a specific column from a specific upstream node."""
    node: Any  # 'Node' — forward ref
    column: str


# Global name counter (used when no workflow context is active)
_name_counter_lock = threading.Lock()
_name_counters: dict[str, int] = {}
_active_workflow: Any = None
_active_workflow_lock = threading.Lock()


def _reset_name_counters() -> None:
    global _name_counters
    _name_counters = {}


def _get_next_name(tool_name: str) -> str:
    """Generate the next auto-name for a tool."""
    with _name_counter_lock:
        _name_counters.setdefault(tool_name, 0)
        _name_counters[tool_name] += 1
        return f"{tool_name}_{_name_counters[tool_name]}"


def get_active_workflow() -> Any:
    with _active_workflow_lock:
        return _active_workflow


def set_active_workflow(wf: Any) -> None:
    global _active_workflow
    with _active_workflow_lock:
        _active_workflow = wf


class Node:
    """A node in the computation DAG. Wraps a tool and its configuration."""

    def __init__(
        self,
        tool: BaseTool,
        kwargs: dict[str, Any] | None = None,
        args: list[Any] | None = None,
        name: str | None = None,
    ) -> None:
        self.tool = tool
        self._kwargs = kwargs or {}
        self._args: list[Any] = args or []
        self._upstream_nodes: set[Node] = set()
        self._column_bindings: dict[str, ColumnRef] = {}
        self._constant_bindings: dict[str, Any] = {}

        # Determine name
        if name is not None:
            self._name = name
        else:
            self._name = _get_next_name(tool.name)

        # Register with active workflow
        wf = get_active_workflow()
        if wf is not None:
            if name is not None and name in wf._nodes:
                raise ValueError(
                    f"Node name '{name}' is not unique. Each node in a Workflow "
                    f"must have a unique name."
                )
            wf._register_node(self)

        # Track upstream from positional args (DataFrameTool)
        for arg in self._args:
            if isinstance(arg, Node):
                self._upstream_nodes.add(arg)

        # Process keyword arguments
        self._process_kwargs()

    def _process_kwargs(self) -> None:
        """Validate and categorize keyword arguments."""
        from bioimageflow.dataframe_tool import DataFrameTool
        from bioimageflow.template import validate_template, get_output_templates

        input_annotations = self.tool.Inputs._get_all_annotations()

        for key, value in self._kwargs.items():
            if key in input_annotations:
                if isinstance(value, ColumnRef):
                    self._column_bindings[key] = value
                    self._upstream_nodes.add(value.node)
                    # Type compatibility check
                    self._check_type_compat(key, value)
                elif isinstance(value, Node):
                    # Node shorthand: field=node -> field=node["field"]
                    col_ref = value[key]  # This will raise ColumnNotFoundError if missing
                    self._column_bindings[key] = col_ref
                    self._upstream_nodes.add(value)
                    self._check_type_compat(key, col_ref)
                else:
                    self._constant_bindings[key] = value
            else:
                raise BindingError(
                    f"Unknown or unexpected keyword argument '{key}' for tool "
                    f"'{self.tool.name}'. Available input fields: "
                    f"{list(input_annotations.keys())}"
                )

        # Check for missing required fields (no default, no binding)
        for field_name, annotation in input_annotations.items():
            if field_name in self._column_bindings:
                continue
            if field_name in self._constant_bindings:
                continue
            if hasattr(self.tool.Inputs, field_name):
                continue  # Has default
            raise BindingError(
                f"Missing required input '{field_name}' for tool '{self.tool.name}'. "
                f"Binding error: no column reference, constant, or default provided."
            )

        # Validate output templates for ProcessingTool
        if isinstance(self.tool, ProcessingTool) and 'Outputs' in type(self.tool).__dict__:
            outputs_cls = self.tool.Outputs
            if hasattr(outputs_cls, '_get_all_annotations'):
                templates = get_output_templates(outputs_cls, self.tool.Inputs)
                for field_name, template in templates.items():
                    validate_template(template, input_annotations)

    def _check_type_compat(self, input_field: str, col_ref: ColumnRef) -> None:
        """Check type compatibility between upstream output and this input."""
        input_annotations = self.tool.Inputs._get_all_annotations()
        consumer_spec = extract_image_spec(input_annotations.get(input_field))
        if consumer_spec is None:
            return

        upstream_tool = col_ref.node.tool
        if not hasattr(upstream_tool, 'Outputs') or 'Outputs' not in type(upstream_tool).__dict__:
            pass

        upstream_outputs = getattr(upstream_tool, 'Outputs', None)
        if upstream_outputs is None:
            return

        output_annotations = upstream_outputs._get_all_annotations()
        if col_ref.column not in output_annotations:
            return  # Will be caught elsewhere

        producer_spec = extract_image_spec(output_annotations[col_ref.column])
        if producer_spec is None:
            return

        if not check_compatibility(producer_spec, consumer_spec):
            raise BindingError(
                f"Type mismatch: upstream '{col_ref.node.name}'.'{col_ref.column}' "
                f"is not compatible with input '{input_field}' of tool "
                f"'{self.tool.name}'. Producer semantics: {producer_spec.semantics}, "
                f"consumer semantics: {consumer_spec.semantics}."
            )

    @property
    def name(self) -> str:
        return self._name

    def __getitem__(self, column: str) -> ColumnRef:
        """Create a ColumnRef: node['column_name']."""
        from bioimageflow.dataframe_tool import DataFrameTool, Passthrough

        # Validate column exists if tool has known Outputs
        tool = self.tool
        has_own_outputs = 'Outputs' in type(tool).__dict__

        if has_own_outputs:
            outputs_cls = tool.Outputs
            output_annotations = outputs_cls._get_all_annotations()

            # For Passthrough, we can't validate columns at construction time
            # (they depend on upstream)
            if not issubclass(outputs_cls, Passthrough):
                if column not in output_annotations:
                    available = list(output_annotations.keys())
                    close = get_close_matches(column, available, n=3, cutoff=0.4)
                    msg = (
                        f"Column '{column}' not found in outputs of node "
                        f"'{self.name}' (tool '{tool.name}'). "
                        f"Available columns: {available}."
                    )
                    if close:
                        msg += f" Did you mean: {', '.join(close)}?"
                    raise ColumnNotFoundError(msg)

        return ColumnRef(node=self, column=column)

    def compute(self, **kwargs: Any) -> Any:
        """Shorthand: create/use a Workflow and compute this node."""
        wf = get_active_workflow()
        if wf is None:
            from bioimageflow.workflow import Workflow
            wf = Workflow()
        return wf.compute(self, **kwargs)
