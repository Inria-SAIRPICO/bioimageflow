"""Focused methods extracted from the workflow façade."""

# Pyright checks the complete contract on Workflow; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from .common import (
    Any,
    Iterator,
    ValidationError,
    _error_capture,
    contextmanager,
)


class _InspectionMixin:
    def topological_order(self) -> list[str]:
        """Return node names in dependency order. Raises on cycle.

        Thin wrapper over :func:`bioimageflow.engine.topological_order`.
        If the graph may contain a cycle, call :meth:`validate` first.
        """
        from bioimageflow.engine import topological_order as _topo

        return _topo(self)

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
            return [
                ValidationError(
                    kind="construction_failed",
                    message=(
                        "Recursive workflow containment is not allowed on one "
                        "active definition path."
                    ),
                )
            ]
        recursion_stack = (*_recursion_stack, id(self))
        errors: list[ValidationError] = []

        if not self.name or "/" in self.name or not isinstance(self.display_name, str):
            errors.append(
                ValidationError(
                    kind="construction_failed",
                    message="Invalid workflow definition metadata.",
                )
            )

        interface_ids: set[str] = set()
        interface_names: set[str] = set()
        for port in [
            *self._interface_inputs.values(),
            *self._interface_outputs.values(),
        ]:
            if (
                not port.id
                or port.id in interface_ids
                or not port.name
                or port.name in interface_names
                or port.name == "name"
            ):
                errors.append(
                    ValidationError(
                        kind="duplicate_name",
                        message=f"Workflow interface ID or name is not unique: '{port.name}'.",
                        field=port.name,
                    )
                )
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
                    errors.append(
                        ValidationError(
                            kind="missing_input",
                            message=f"Workflow input '{port.name}' has an unknown target.",
                            field=port.name,
                        )
                    )
                    continue
                endpoint_kind = endpoint.get("kind")
                if endpoint_kind != "workflow" and (
                    (port.kind == "field") != (endpoint_kind == "field")
                ):
                    errors.append(
                        ValidationError(
                            kind="type_mismatch",
                            message=f"Workflow input '{port.name}' has an incompatible target kind.",
                            node=target_node.name,
                            field=port.name,
                        )
                    )
                if endpoint_kind == "workflow":
                    from bioimageflow.workflow_node import WorkflowNode

                    child_id = endpoint.get("id")
                    child_port = None
                    if isinstance(target_node, WorkflowNode) and isinstance(
                        child_id, str
                    ):
                        child_port = target_node.workflow._interface_inputs.get(
                            child_id
                        )
                    if child_port is None or child_port.kind != port.kind:
                        errors.append(
                            ValidationError(
                                kind="type_mismatch",
                                message=f"Workflow input '{port.name}' has an incompatible child port.",
                                node=target_node.name,
                                field=port.name,
                            )
                        )

        for port in self._interface_outputs.values():
            source_node = self._nodes.get(port.source_node)
            if source_node is None:
                errors.append(
                    ValidationError(
                        kind="column_not_found",
                        message=f"Workflow output '{port.name}' has an unknown source node.",
                        field=port.name,
                    )
                )
                continue
            output_schema = source_node.get_output_schema()
            if output_schema is not None and port.source_output not in output_schema:
                errors.append(
                    ValidationError(
                        kind="column_not_found",
                        message=f"Workflow output '{port.name}' has an unknown source column.",
                        node=source_node.name,
                        field=port.name,
                    )
                )

        for registered_name, registered_node in self._nodes.items():
            if (
                not registered_name
                or "/" in registered_name
                or registered_node._name != registered_name
            ):
                errors.append(
                    ValidationError(
                        kind="duplicate_name",
                        message=f"Invalid structural node identity '{registered_name}'.",
                        node=registered_name,
                    )
                )

        # Step 1: cycle detection (doesn't block the rest — we still check parameters).
        try:
            topological_order(self)
        except CycleError as exc:
            cycle = exc.args[1] if len(exc.args) > 1 else []
            errors.append(
                ValidationError(
                    kind="cycle",
                    message=f"Cycle detected: {cycle}",
                )
            )

        for name, node in self._nodes.items():
            if isinstance(node, WorkflowNode):
                unknown_ports = node._bound_port_ids() - set(
                    node.workflow._interface_inputs
                )
                for port_id in unknown_ports:
                    errors.append(
                        ValidationError(
                            kind="unknown_input",
                            message=f"Unknown workflow input port '{port_id}'.",
                            node=name,
                            field=port_id,
                        )
                    )
                for port in node.workflow._interface_inputs.values():
                    if port.id not in node._bound_port_ids() and not port.has_fallback(
                        node.workflow
                    ):
                        errors.append(
                            ValidationError(
                                kind="missing_input",
                                message=(
                                    f"Missing required input '{port.name}' for workflow "
                                    f"'{node.workflow.name}'."
                                ),
                                node=name,
                                field=port.name,
                            )
                        )
                for e in node.workflow.validate(_recursion_stack=recursion_stack):
                    errors.append(
                        ValidationError(
                            kind=e.kind,
                            message=e.message,
                            node=e.node,
                            field=e.field,
                            edge=e.edge,
                            edge_id=e.edge_id,
                            path=(name, *e.path),
                        )
                    )
                continue

            # Step 2: type compatibility on column bindings.
            for field, col_ref in node._column_bindings.items():
                eid = node._column_binding_edge_ids.get(field)
                err = check_type_compat(node, field, col_ref)
                if err is not None:
                    if eid is not None and err.edge_id is None:
                        err = ValidationError(
                            kind=err.kind,
                            message=err.message,
                            node=err.node,
                            field=err.field,
                            edge=err.edge,
                            edge_id=eid,
                            path=err.path,
                        )
                    errors.append(err)
                # Column-not-found on bindings recorded at the structural level.
                upstream_outputs = col_ref.node.tool.Outputs
                if upstream_outputs is not None:
                    from bioimageflow.dataframe_tool import Passthrough

                    output_annotations = upstream_outputs._get_all_annotations()
                    if (
                        not issubclass(upstream_outputs, Passthrough)
                        and col_ref.column not in output_annotations
                    ):
                        errors.append(
                            ValidationError(
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
                            )
                        )

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
                errors.append(
                    ValidationError(
                        kind="missing_input",
                        message=(
                            f"Missing required input '{field_name}' for tool "
                            f"'{type(node.tool).__name__}'."
                        ),
                        node=name,
                        field=field_name,
                    )
                )

            # Step 4: Pydantic validation of supplied constants.
            try:
                param_errors = validate_parameters(
                    type(node.tool),
                    node._constant_bindings,
                    node=name,
                )
            except Exception as exc:  # pragma: no cover — defensive
                param_errors = [
                    ValidationError(
                        kind="construction_failed",
                        message=f"Pydantic validation setup failed: {exc}",
                        node=name,
                    )
                ]
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

            wf = Workflow(storage_path="./results")
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
