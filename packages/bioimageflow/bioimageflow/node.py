"""Node and ColumnRef — graph construction primitives."""

import contextvars
import copy
from contextlib import contextmanager
import threading
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path
from typing import Any, TYPE_CHECKING

from bioimageflow_core.tool import ProcessingTool, BaseTool
from bioimageflow.validation import (
    ValidationError,
    ValidationErrorKind,
    extract_image_spec,
)
from bioimageflow_core.types import check_compatibility
from bioimageflow_core.viewer import (
    ViewerSpec,
    coerce_viewer_spec,
    merge_viewer_specs,
)

if TYPE_CHECKING:
    from bioimageflow.resources import NodeResourceOverrides
    from bioimageflow.workflow.capture import CapturedOutputDeclaration
    from bioimageflow_core import ResourceSpec


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

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field

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
            field=field if field is not None else self.field,
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


class SourceToolUpstreamError(Exception):
    """Raised when a source DataFrameTool (``accepts_upstream = False``)
    is constructed with positional upstream arguments.
    """

    def to_validation_error(
        self,
        node: str | None = None,
        field: str | None = None,
    ) -> ValidationError:
        return ValidationError(
            kind="source_tool_upstream",
            message=str(self),
            node=node,
            field=field,
        )


# ── Error-capture ContextVar ────────────────────────────────────────────
# When set to a list, node-construction errors are appended to it instead
# of being raised. See Workflow.capture_errors() for the public API.
_error_capture: contextvars.ContextVar[list[ValidationError] | None] = (
    contextvars.ContextVar("_bif_error_capture", default=None)
)


def _get_error_capture() -> list[ValidationError] | None:
    return _error_capture.get()


@dataclass(frozen=True)
class ColumnRef:
    """References a specific column from a specific upstream node."""
    node: Any  # 'Node' — forward ref
    column: str


# Global name counter (used when no workflow context is active)
_name_counter_lock = threading.Lock()
_name_counters: dict[str, int] = {}
_active_workflow: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "_bif_active_workflow",
    default=None,
)
_runtime_node_names: contextvars.ContextVar[dict[int, str]] = contextvars.ContextVar(
    "_bif_runtime_node_names",
    default={},
)


@contextmanager
def scoped_node_names(names: dict["Node", str]):
    """Expose immutable execution paths without changing structural names."""
    merged = dict(_runtime_node_names.get())
    merged.update({id(node): name for node, name in names.items()})
    token = _runtime_node_names.set(merged)
    try:
        yield
    finally:
        _runtime_node_names.reset(token)


def _reset_name_counters() -> None:
    global _name_counters
    _name_counters = {}


def _get_next_name(tool_name: str) -> str:
    """Choose a workflow-local name without consuming it before admission."""
    workflow = get_active_workflow()
    if workflow is not None:
        index = 1
        while f"{tool_name}_{index}" in workflow._nodes:
            index += 1
        return f"{tool_name}_{index}"
    with _name_counter_lock:
        _name_counters.setdefault(tool_name, 0)
        _name_counters[tool_name] += 1
        return f"{tool_name}_{_name_counters[tool_name]}"


def get_active_workflow() -> Any:
    return _active_workflow.get()


def set_active_workflow(wf: Any) -> contextvars.Token:
    return _active_workflow.set(wf)


def reset_active_workflow(token: contextvars.Token) -> None:
    _active_workflow.reset(token)


class Node:
    """A node in the computation DAG. Wraps a tool and its configuration."""

    _captured_output_declaration: "CapturedOutputDeclaration"

    def __init__(
        self,
        tool: BaseTool,
        kwargs: dict[str, Any] | None = None,
        args: list[Any] | None = None,
        name: str | None = None,
        output_templates: dict[str, str] | None = None,
        resource_overrides: "NodeResourceOverrides | None" = None,
        viewer_additions: dict[str, Any] | None = None,
    ) -> None:
        from bioimageflow.resources import NodeResourceOverrides

        if isinstance(tool, ProcessingTool):
            from bioimageflow_core import EnvironmentSpec
            if tool.Outputs is None or not isinstance(getattr(tool, "environment", None), EnvironmentSpec):
                raise TypeError("Concrete ProcessingTool graph nodes require Outputs and an EnvironmentSpec.")
        self.tool = tool
        self._kwargs = dict(kwargs or {})
        self._args: list[Any] = list(args or [])
        self.output_templates: dict[str, str] = dict(output_templates or {})
        self._viewer_additions: dict[str, ViewerSpec] = {}
        for output, addition in (viewer_additions or {}).items():
            if not isinstance(output, str) or not output:
                raise ValueError("Viewer addition output keys must be non-empty strings.")
            self._viewer_additions[output] = coerce_viewer_spec(addition)
        if resource_overrides is not None:
            if not isinstance(tool, ProcessingTool):
                raise TypeError(
                    "resource_overrides apply only to ProcessingTool nodes."
                )
            if not isinstance(resource_overrides, NodeResourceOverrides):
                raise TypeError(
                    "resource_overrides must be NodeResourceOverrides or None."
                )
            resource_overrides.effective(getattr(tool, "resources", None))
        self._resource_overrides: NodeResourceOverrides | None = resource_overrides
        self.enabled: bool = True
        self._upstream_nodes: set[Node] = set()
        self._column_bindings: dict[str, ColumnRef] = {}
        self._constant_bindings: dict[str, Any] = {}
        # Optional opaque edge identifiers, parallel to _column_bindings
        # and _args. Populated by Workflow._reconstruct_from_dict from
        # the ``id`` key of edge dicts; ``None`` for programmatic
        # construction. See plan-platform-boundary-refactor.md Task 1.
        self._column_binding_edge_ids: dict[str, str | None] = {}
        self._arg_edge_ids: list[str | None] = [None] * len(self._args)
        self._workflow_input_bindings: dict[str, Any] = {}
        self._workflow_dataframe_bindings: dict[int, Any] = {}
        self._workflow_input_fallback_constants: set[str] = set()
        self._pending_interface_targets: list[tuple[Any, dict[str, Any]]] = []

        # Determine name
        if name is not None:
            if not name or "/" in name:
                raise ValueError("Node names must be non-empty and may not contain '/'.")
            self._name = name
        else:
            self._name = _get_next_name(type(tool).__name__)

        # Register with active workflow
        wf = get_active_workflow()
        capture = _get_error_capture()
        capture_start = len(capture) if capture is not None else 0
        if capture is None:
            # Reject prohibited bindings before positional or keyword symbolic
            # inputs can publish targets into their owning workflow.
            declared_inputs = self.tool.Inputs._get_all_annotations()
            for field, value in self._kwargs.items():
                if field in declared_inputs and isinstance(value, (ColumnRef, Node)):
                    self._check_column_binding_allowed(field)
        if wf is not None:
            if name is not None and name in wf._nodes:
                if capture is not None:
                    capture.append(ValidationError(
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

        # Track upstream from positional args (DataFrameTool)
        for index, arg in enumerate(self._args):
            from bioimageflow.workflow import WorkflowInputRef
            if isinstance(arg, WorkflowInputRef):
                if arg.kind != "dataframe":
                    raise BindingError(
                        f"Field workflow input '{arg.name}' cannot be used as a positional DataFrame input."
                    )
                arg.workflow._bind_input_target(
                    arg, self, index, kind="dataframe",
                )
                self._workflow_dataframe_bindings[index] = arg
                self._args[index] = None
                continue
            if isinstance(arg, Node):
                self._upstream_nodes.add(arg)

        # New-node state and symbolic targets stay private until every
        # positional/keyword/template/schema admission has completed.
        self._process_kwargs()
        self._construction_errors = [] if capture is None else list(capture[capture_start:])
        if wf is not None:
            wf._register_node(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> "Node":
        existing = memo.get(id(self))
        if existing is not None:
            return existing
        clone = object.__new__(type(self))
        memo[id(self)] = clone
        # The executable selector is carried, not claimed to be captured code.
        # A tool's locks/model caches are execution state, not definition data.
        clone.tool = copy.copy(self.tool)
        if isinstance(self.tool, ProcessingTool):
            setattr(clone.tool, "environment", copy.deepcopy(self.tool.environment, memo))
        from bioimageflow.workflow.capture import capture_model, capture_output_declaration, capture_value
        output_declaration = capture_output_declaration(
            self.tool.Outputs, getattr(self, "_captured_output_declaration", None),
        )
        setattr(clone.tool, "Inputs", capture_model(self.tool.Inputs))
        setattr(clone.tool, "Outputs", output_declaration.frozen_model)
        for key, value in self.__dict__.items():
            if key in {"tool", "_captured_output_declaration"}:
                continue
            if key == "_constant_bindings":
                captured = {name: capture_value(item) for name, item in value.items()}
            elif key == "_kwargs":
                captured = {name: copy.deepcopy(item, memo) if isinstance(item, (Node, ColumnRef)) or hasattr(item, "port_id") else capture_value(item) for name, item in value.items()}
            elif key == "_args":
                captured = [copy.deepcopy(item, memo) if isinstance(item, Node) or item is None else capture_value(item) for item in value]
            else:
                captured = copy.deepcopy(value, memo)
            setattr(clone, key, captured)
        clone._captured_output_declaration = output_declaration
        clone._capture_defaults()
        return clone

    def _capture_defaults(self) -> None:
        from bioimageflow_core.defaults import snapshot_value
        for field, value in self.tool.Inputs.capture_defaults().items():
            if field not in self._constant_bindings and field not in self._column_bindings:
                self._constant_bindings[field] = snapshot_value(value)
                if field in self._workflow_input_bindings:
                    self._workflow_input_fallback_constants.add(field)

    def _refresh_dependencies(self) -> None:
        self._upstream_nodes = {
            reference.node for reference in self._column_bindings.values()
        } | {value for value in self._args if isinstance(value, Node)}

    def _check_column_binding_allowed(self, field: str) -> None:
        """Reject row-valued bindings where a whole-table tool needs a constant."""
        from bioimageflow.dataframe_tool import DataFrameTool

        if isinstance(self.tool, DataFrameTool):
            raise BindingError(
                f"DataFrameTool '{type(self.tool).__name__}' input '{field}' "
                "requires a constant; column references and Node shorthand are "
                "not allowed. Pass upstream DataFrames as positional arguments.",
                field=field,
            )

    def _process_kwargs(self) -> None:
        """Validate and categorize keyword arguments.

        When an error-capture buffer is active (via ``Workflow.capture_errors``),
        per-kwarg exceptions are appended as :class:`ValidationError` and
        processing continues so the GUI can surface every problem in one
        pass. Without a capture buffer, the first error is raised.
        """
        from bioimageflow.template import validate_template, get_output_templates

        input_annotations = self.tool.Inputs._get_all_annotations()
        capture = _get_error_capture()

        for key, value in self._kwargs.items():
            try:
                if key in input_annotations:
                    from bioimageflow.workflow import WorkflowInputRef
                    if isinstance(value, WorkflowInputRef):
                        if value.kind != "field":
                            raise BindingError(
                                f"DataFrame workflow input '{value.name}' cannot target named field '{key}'."
                            )
                        value.workflow._bind_input_target(
                            value, self, key, kind="field",
                        )
                        self._workflow_input_bindings[key] = value
                    elif isinstance(value, ColumnRef):
                        self._check_column_binding_allowed(key)
                        self._column_bindings[key] = value
                        self._upstream_nodes.add(value.node)
                        # Type compatibility check
                        self._check_type_compat(key, value)
                    elif isinstance(value, Node):
                        self._check_column_binding_allowed(key)
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
                if capture is None:
                    raise
                # Distinguish "unknown kwarg" from type-mismatch/missing by
                # checking whether the key is a declared input.
                if key not in input_annotations:
                    capture.append(exc.to_validation_error(
                        self._name, field=key, kind="unknown_input",
                    ))
                else:
                    capture.append(exc.to_validation_error(
                        self._name, field=key, kind="type_mismatch",
                    ))
            except ColumnNotFoundError as exc:
                if capture is None:
                    raise
                capture.append(exc.to_validation_error(self._name, field=key))

        # Check for missing required fields (no default, no binding)
        for field_name, annotation in input_annotations.items():
            if field_name in self._column_bindings:
                continue
            if field_name in self._constant_bindings:
                continue
            if field_name in self._workflow_input_bindings:
                continue
            if hasattr(self.tool.Inputs, field_name):
                continue  # Has default
            exc = BindingError(
                f"Missing required input '{field_name}' for tool '{type(self.tool).__name__}'. "
                f"Binding error: no column reference, constant, or default provided."
            )
            if capture is None:
                raise exc
            capture.append(exc.to_validation_error(self._name, field=field_name))

        # Validate output templates for ProcessingTool
        if isinstance(self.tool, ProcessingTool) and self.tool.Outputs is not None:
            outputs_cls = self.tool.Outputs
            if hasattr(outputs_cls, '_get_all_annotations'):
                templates = get_output_templates(outputs_cls, self.tool.Inputs)
                for field_name, template in self.output_templates.items():
                    if template == "":
                        continue
                    if field_name not in templates:
                        exc = ValueError(
                            f"Output template references unknown path output "
                            f"'{field_name}'. Available path outputs: "
                            f"{list(templates.keys())}"
                        )
                        if capture is None:
                            raise exc
                        capture.append(ValidationError(
                            kind="construction_failed",
                            message=str(exc),
                            node=self._name,
                            field=field_name,
                        ))
                        continue
                    if not isinstance(template, str):
                        exc = TypeError(
                            f"Output template for '{field_name}' must be a string."
                        )
                        if capture is None:
                            raise exc
                        capture.append(ValidationError(
                            kind="construction_failed",
                            message=str(exc),
                            node=self._name,
                            field=field_name,
                        ))
                        continue
                    templates[field_name] = template
                for field_name, template in templates.items():
                    try:
                        validate_template(template, input_annotations)
                    except Exception as exc:
                        if capture is None:
                            raise
                        capture.append(ValidationError(
                            kind="construction_failed",
                            message=str(exc),
                            node=self._name,
                            field=field_name,
                        ))

        schema = self.get_output_schema()
        if schema is not None and "_passthrough" not in schema:
            unknown_viewers = set(self._viewer_additions) - set(schema)
            if unknown_viewers:
                exc = ValueError(
                    "Viewer additions reference unknown output fields: "
                    f"{sorted(unknown_viewers)}."
                )
                if capture is None:
                    raise exc
                for field_name in sorted(unknown_viewers):
                    capture.append(ValidationError(
                        kind="construction_failed",
                        message=str(exc),
                        node=self._name,
                        field=field_name,
                    ))

    @property
    def resource_overrides(self) -> "NodeResourceOverrides | None":
        """Return this node instance's portable worker resource overrides."""
        return self._resource_overrides

    def set_resource_overrides(
        self,
        value: "NodeResourceOverrides | None",
    ) -> "Node":
        """Set validated worker overrides and return this node."""
        from bioimageflow.resources import NodeResourceOverrides

        if not isinstance(self.tool, ProcessingTool):
            raise TypeError("Worker resources apply only to ProcessingTool nodes.")
        if value is not None and not isinstance(value, NodeResourceOverrides):
            raise TypeError("value must be NodeResourceOverrides or None.")
        if value is not None:
            value.effective(getattr(self.tool, "resources", None))
        self._resource_overrides = value
        return self

    @property
    def effective_resources(self) -> "ResourceSpec":
        """Return the declaration/override merge for this node."""
        from bioimageflow.resources import effective_node_resources

        return effective_node_resources(self)

    @property
    def viewer_additions(self) -> dict[str, ViewerSpec]:
        """Return this node's portable additive per-output viewer metadata."""
        return dict(self._viewer_additions)

    def set_viewer_addition(self, output: str, value: Any | None) -> "Node":
        """Set or clear a portable viewer addition and return this node."""
        if not isinstance(output, str) or not output:
            raise ValueError("Viewer addition output must be a non-empty string.")
        schema = self.get_output_schema()
        if schema is not None and "_passthrough" not in schema and output not in schema:
            raise ValueError(f"Unknown output field {output!r} on node {self.name!r}.")
        if value is None:
            self._viewer_additions.pop(output, None)
        else:
            self._viewer_additions[output] = coerce_viewer_spec(value)
        return self

    def get_output_viewer_spec(self, output: str) -> ViewerSpec | None:
        """Return the tool declaration plus this node's additive declaration."""
        schema = self.get_output_schema()
        if schema is not None and output in schema:
            encoded = schema[output].get("viewer")
            if encoded is not None:
                return ViewerSpec.from_dict(encoded)
        return self._viewer_additions.get(output)

    def get_output_viewer_specs(self) -> dict[str, ViewerSpec]:
        """Return every known non-empty effective output viewer declaration."""
        schema = self.get_output_schema() or {}
        outputs = (set(schema) - {"_passthrough"}) | set(self._viewer_additions)
        return {
            output: spec
            for output in sorted(outputs)
            if (spec := self.get_output_viewer_spec(output)) is not None
        }

    def _check_type_compat(self, input_field: str, col_ref: ColumnRef) -> None:
        """Check type compatibility between upstream output and this input."""
        input_annotations = self.tool.Inputs._get_all_annotations()
        consumer_spec = extract_image_spec(input_annotations.get(input_field))
        if consumer_spec is None:
            return

        producer = col_ref.node.get_resolved_output_schema().get(col_ref.column)
        producer_spec = None if producer is None else producer.image_spec
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
        return _runtime_node_names.get().get(id(self), self._name)

    def _resolve_output_columns(self) -> dict[str, dict[str, Any]] | None:
        """Resolve this node's output column schema as currently configured.

        Algorithm:

        1. ``DataFrameTool`` with a ``resolve_merge_schema`` override
           (built-in merge tools): collect upstream schemas via each
           positional arg's ``get_output_schema()`` and call
           ``tool.resolve_merge_schema(upstream_schemas, kwargs)``.
        2. ``DataFrameTool`` (or subclass) with ``resolve_outputs`` →
           ``tool.resolve_outputs(kwargs)``.
        3. ``ProcessingTool`` → static
           ``serialize_output_schema(type(tool))``.

        Returns ``None`` when the schema is unresolvable (any required
        upstream returns ``None``, or the tool has no ``Outputs`` and no
        override). Idempotent and side-effect free.
        """
        from bioimageflow.dataframe_tool import DataFrameTool
        from bioimageflow.validation import (
            _overrides_classmethod,
            serialize_output_schema,
        )

        def _overrides_resolve_merge_schema(cls: type) -> bool:
            return _overrides_classmethod(cls, DataFrameTool, "resolve_merge_schema")

        if isinstance(self.tool, DataFrameTool):
            df_tool_cls: type[DataFrameTool] = type(self.tool)
            # _constant_bindings holds exactly the kwargs that aren't
            # ColumnRefs/Nodes (see Node._process_kwargs); it's the right
            # input dict for resolve_outputs / resolve_merge_schema.
            if _overrides_resolve_merge_schema(df_tool_cls):
                upstream_schemas = [
                    arg.get_output_schema() if isinstance(arg, Node) else None
                    for arg in self._args
                ]
                schema = df_tool_cls.resolve_merge_schema(
                    upstream_schemas, self._constant_bindings,
                )
            elif _overrides_classmethod(df_tool_cls, DataFrameTool, "resolve_outputs"):
                schema = df_tool_cls.resolve_outputs(self._constant_bindings)
            else:
                schema = (
                    None if self.tool.Outputs is None
                    else serialize_output_schema(self.tool)
                )
            return self._schema_with_viewer_additions(schema)

        # ProcessingTool: static schema.
        if getattr(self.tool, "Outputs", None) is None:
            return None
        return self._schema_with_viewer_additions(serialize_output_schema(self.tool))

    def get_resolved_output_schema(self) -> Any:
        from bioimageflow.validation.resolved import ResolvedSchema
        from bioimageflow.validation import _overrides_classmethod
        outputs = getattr(self.tool, "Outputs", None)
        from bioimageflow.dataframe_tool import DataFrameTool
        configured = isinstance(self.tool, DataFrameTool) and any(
            _overrides_classmethod(type(self.tool), DataFrameTool, method)
            for method in ("resolve_outputs", "resolve_merge_schema")
        )
        annotations = outputs._get_all_annotations() if outputs is not None and not configured else {}
        columns = self._resolve_output_columns()
        if columns is not None and "_passthrough" in columns:
            columns = dict(columns)
            columns.pop("_passthrough")
            declared = dict(columns)
            inherited: dict[str, Any] = {}
            inherited_annotations: dict[str, Any] = {}
            for argument in self._args:
                if not isinstance(argument, Node):
                    partial = ResolvedSchema.from_columns(declared, annotations=annotations)
                    return ResolvedSchema("dynamic", tuple(partial.ports.items()))
                upstream = argument.get_resolved_output_schema()
                if upstream.state == "dynamic":
                    partial = ResolvedSchema.from_columns(declared, annotations=annotations)
                    return ResolvedSchema("dynamic", tuple(partial.ports.items()))
                inherited.update(upstream.to_wire() or {})
                inherited_annotations.update({name: port.annotation for name, port in upstream.ports.items()})
            inherited.update(declared)
            inherited_annotations.update(annotations)
            columns, annotations = inherited, inherited_annotations
        return ResolvedSchema.from_columns(columns, annotations=annotations)

    def get_output_schema(self) -> dict[str, dict[str, Any]] | None:
        """Return a detached portable projection of resolved semantic columns."""
        return self.get_resolved_output_schema().to_wire()

    def _schema_with_viewer_additions(
        self,
        schema: dict[str, dict[str, Any]] | None,
    ) -> dict[str, dict[str, Any]] | None:
        if schema is None:
            return None
        result = copy.deepcopy(schema)
        for output, addition in self._viewer_additions.items():
            if output not in result or output == "_passthrough":
                continue
            declared = result[output].get("viewer")
            combined = merge_viewer_specs(
                None if declared is None else ViewerSpec.from_dict(declared),
                addition,
            )
            if combined is not None:
                result[output]["viewer"] = combined.to_dict()
        return result

    def __getitem__(self, column: str) -> ColumnRef:
        """Select a column against the same resolved authority as consumers."""
        schema = self.get_resolved_output_schema()
        if schema.state == "known" and schema.get(column) is None:
            available = list(schema.ports)
            close = get_close_matches(column, available, n=3, cutoff=0.4)
            message = (
                f"Column '{column}' not found in outputs of node '{self.name}' "
                f"(tool '{type(self.tool).__name__}'). Available columns: {available}."
            )
            if close:
                message += f" Did you mean: {', '.join(close)}?"
            error = ColumnNotFoundError(message)
            capture = _get_error_capture()
            if capture is None:
                raise error
            capture.append(error.to_validation_error(self._name))
        return ColumnRef(node=self, column=column)

    def disable(self) -> None:
        """Disable this node so it is skipped during execution."""
        self.enabled = False

    def enable(self) -> None:
        """Re-enable this node for execution."""
        self.enabled = True

    def compute(
        self,
        *,
        storage_path: str | Path | None = None,
        **kwargs: Any,
    ) -> Any:
        """Compute in the active workflow or create one with explicit storage."""
        wf = get_active_workflow()
        if wf is None:
            from bioimageflow.workflow import Workflow
            if storage_path is None:
                raise TypeError(
                    "Node.compute() requires storage_path when no Workflow is active."
                )
            wf = Workflow(storage_path=storage_path)
        elif storage_path is not None:
            raise TypeError(
                "Node.compute() cannot replace the active Workflow storage_path."
            )
        return wf.compute(self, **kwargs)
