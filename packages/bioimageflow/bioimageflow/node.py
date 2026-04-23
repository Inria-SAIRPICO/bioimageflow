"""Node and ColumnRef — graph construction primitives."""

import contextvars
import threading
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any

from bioimageflow_core.tool import ProcessingTool, BaseTool
from bioimageflow.validation import (
    ValidationError,
    ValidationErrorKind,
    extract_image_spec,
)
from bioimageflow_core.types import check_compatibility


class ColumnNotFoundError(Exception):
    """Raised when a column reference targets a non-existent column."""

    def to_validation_error(
        self,
        node: str,
        field: str | None = None,
    ) -> ValidationError:
        return ValidationError(
            kind="column_not_found",
            message=str(self),
            node=node,
            field=field,
        )


class BindingError(Exception):
    """Raised when a required input field has no source."""

    def to_validation_error(
        self,
        node: str,
        field: str | None = None,
        kind: ValidationErrorKind = "missing_input",
    ) -> ValidationError:
        return ValidationError(
            kind=kind,
            message=str(self),
            node=node,
            field=field,
        )


class IndexAlignmentError(Exception):
    """Raised when upstream indices are incompatible."""

    def to_validation_error(
        self,
        node: str,
        field: str | None = None,
    ) -> ValidationError:
        return ValidationError(
            kind="construction_failed",
            message=str(self),
            node=node,
            field=field,
        )


# ── Error-collector ContextVar ──────────────────────────────────────────
# When set to a list, node-construction errors are appended to it instead
# of being raised. See Workflow.collect_errors() for the public API.
_error_collector: contextvars.ContextVar[list[ValidationError] | None] = (
    contextvars.ContextVar("_bif_error_collector", default=None)
)


def _get_error_collector() -> list[ValidationError] | None:
    return _error_collector.get()


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
        self.enabled: bool = True
        self._upstream_nodes: set[Node] = set()
        self._column_bindings: dict[str, ColumnRef] = {}
        self._constant_bindings: dict[str, Any] = {}

        # Determine name
        if name is not None:
            self._name = name
        else:
            self._name = _get_next_name(type(tool).__name__)

        # Register with active workflow
        wf = get_active_workflow()
        collector = _get_error_collector()
        if wf is not None:
            if name is not None and name in wf._nodes:
                if collector is not None:
                    collector.append(ValidationError(
                        kind="duplicate_name",
                        message=(
                            f"Node name '{name}' is not unique. Each node in "
                            f"a Workflow must have a unique name."
                        ),
                        node=name,
                    ))
                    # Skip registration to keep the existing node addressable.
                    return
                raise ValueError(
                    f"Node name '{name}' is not unique. Each node in a Workflow "
                    f"must have a unique name."
                )
            wf._register_node(self)

        # Track upstream from positional args (DataFrameTool)
        for arg in self._args:
            if isinstance(arg, Node):
                self._upstream_nodes.add(arg)

        # Process keyword arguments. When a collector is active we keep the
        # node registered and best-effort wire what we can; otherwise we
        # unregister on failure so the workflow stays clean.
        if collector is not None:
            self._process_kwargs()
        else:
            try:
                self._process_kwargs()
            except Exception:
                if wf is not None:
                    wf._nodes.pop(self._name, None)
                raise

    def _process_kwargs(self) -> None:
        """Validate and categorize keyword arguments.

        When an error-collector is active (via ``Workflow.collect_errors``),
        per-kwarg exceptions are appended as :class:`ValidationError` and
        processing continues so the GUI can surface every problem in one
        pass. Without a collector, the first error is raised (existing
        behavior).
        """
        from bioimageflow.template import validate_template, get_output_templates

        input_annotations = self.tool.Inputs._get_all_annotations()
        collector = _get_error_collector()

        for key, value in self._kwargs.items():
            try:
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
                        f"'{type(self.tool).__name__}'. Available input fields: "
                        f"{list(input_annotations.keys())}"
                    )
            except BindingError as exc:
                if collector is None:
                    raise
                # Distinguish "unknown kwarg" from type-mismatch/missing by
                # checking whether the key is a declared input.
                if key not in input_annotations:
                    collector.append(exc.to_validation_error(
                        self._name, field=key, kind="unknown_input",
                    ))
                else:
                    collector.append(exc.to_validation_error(
                        self._name, field=key, kind="type_mismatch",
                    ))
            except ColumnNotFoundError as exc:
                if collector is None:
                    raise
                collector.append(exc.to_validation_error(self._name, field=key))

        # Check for missing required fields (no default, no binding)
        for field_name, annotation in input_annotations.items():
            if field_name in self._column_bindings:
                continue
            if field_name in self._constant_bindings:
                continue
            if hasattr(self.tool.Inputs, field_name):
                continue  # Has default
            exc = BindingError(
                f"Missing required input '{field_name}' for tool '{type(self.tool).__name__}'. "
                f"Binding error: no column reference, constant, or default provided."
            )
            if collector is None:
                raise exc
            collector.append(exc.to_validation_error(self._name, field=field_name))

        # Validate output templates for ProcessingTool
        if isinstance(self.tool, ProcessingTool) and self.tool.Outputs is not None:
            outputs_cls = self.tool.Outputs
            if hasattr(outputs_cls, '_get_all_annotations'):
                templates = get_output_templates(outputs_cls, self.tool.Inputs)
                for field_name, template in templates.items():
                    try:
                        validate_template(template, input_annotations)
                    except Exception as exc:
                        if collector is None:
                            raise
                        collector.append(ValidationError(
                            kind="construction_failed",
                            message=str(exc),
                            node=self._name,
                            field=field_name,
                        ))

    def _check_type_compat(self, input_field: str, col_ref: ColumnRef) -> None:
        """Check type compatibility between upstream output and this input."""
        input_annotations = self.tool.Inputs._get_all_annotations()
        consumer_spec = extract_image_spec(input_annotations.get(input_field))
        if consumer_spec is None:
            return

        upstream_tool = col_ref.node.tool
        upstream_outputs = upstream_tool.Outputs
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
                f"'{type(self.tool).__name__}'. Producer semantics: {producer_spec.semantics}, "
                f"consumer semantics: {consumer_spec.semantics}."
            )

    @property
    def name(self) -> str:
        return self._name

    def __getitem__(self, column: str) -> ColumnRef:
        """Create a ColumnRef: node['column_name']."""
        from bioimageflow.dataframe_tool import Passthrough

        # Validate column exists if tool has known Outputs
        tool = self.tool
        has_own_outputs = tool.Outputs is not None

        if has_own_outputs:
            outputs_cls = tool.Outputs
            assert outputs_cls is not None  # guarded by has_own_outputs
            output_annotations = outputs_cls._get_all_annotations()

            # For Passthrough, we can't validate columns at construction time
            # (they depend on upstream)
            if not issubclass(outputs_cls, Passthrough):
                if column not in output_annotations:
                    available = list(output_annotations.keys())
                    close = get_close_matches(column, available, n=3, cutoff=0.4)
                    msg = (
                        f"Column '{column}' not found in outputs of node "
                        f"'{self.name}' (tool '{type(tool).__name__}'). "
                        f"Available columns: {available}."
                    )
                    if close:
                        msg += f" Did you mean: {', '.join(close)}?"
                    exc = ColumnNotFoundError(msg)
                    collector = _get_error_collector()
                    if collector is None:
                        raise exc
                    collector.append(exc.to_validation_error(self._name))
                    # Fall through — return a best-effort ColumnRef so
                    # downstream construction can continue collecting.

        return ColumnRef(node=self, column=column)

    def disable(self) -> None:
        """Disable this node so it is skipped during execution."""
        self.enabled = False

    def enable(self) -> None:
        """Re-enable this node for execution."""
        self.enabled = True

    def compute(self, **kwargs: Any) -> Any:
        """Shorthand: create/use a Workflow and compute this node."""
        wf = get_active_workflow()
        if wf is None:
            from bioimageflow.workflow import Workflow
            wf = Workflow()
        return wf.compute(self, **kwargs)
