"""Focused methods extracted from the workflow façade."""

# Pyright checks the complete contract on Workflow; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false, reportCallIssue=false, reportReturnType=false

from __future__ import annotations

from typing import TYPE_CHECKING
from dataclasses import replace

from bioimageflow.node import BindingError
from bioimageflow_core.viewer import ViewerSpec

from .common import (
    Any,
    Callable,
    ColumnRef,
    MISSING,
    Node,
    Path,
    ProgressEvent,
    ValidationError,
    WorkflowInputPort,
    WorkflowInputRef,
    WorkflowOutputPort,
    cast,
    copy,
    deserialize_constant,
    get_active_workflow,
    importlib,
    set_active_workflow,
)
from .custom_sources import (
    _auto_install_if_missing,
    _get_store_path,
    _resolve_custom_tool_class,
)
from .interfaces import _resolve_output_source

if TYPE_CHECKING:
    from .model import Workflow


class _MaterializationMixin:
    @classmethod
    def _materialize_graph(
        cls,
        graph: dict[str, Any],
        *,
        custom_modules: dict[str, Any],
        source_records: list[dict[str, Any]],
        auto_install: bool,
        storage_path: str | Path,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        engine: str | None = None,
        execution: str | None = None,
        wetlands_config: dict[str, Any] | None = None,
        partial: bool = False,
        errors: list[ValidationError] | None = None,
        graph_stack: tuple[int, ...] = (),
        _admission: dict[str, Any] | None = None,
    ) -> "Workflow":
        from graphlib import TopologicalSorter
        from bioimageflow.dataframe_tool import DataFrameTool

        from .graph_admission import capture_graph
        if _admission is None:
            graph, _admission = capture_graph(graph, partial=partial)
        graph_stack = (*graph_stack, id(graph))
        config = graph["config"]
        wf = cls(
            name=graph["name"],
            display_name=graph["display_name"],
            storage_path=storage_path,
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

        from bioimageflow.validation.resolved import ResolvedSchema
        for item in graph["interface"]["inputs"]:
            schema = copy.deepcopy(item.get("schema"))
            annotation = None
            if item["kind"] == "field":
                semantic = ResolvedSchema.from_columns({item["id"]: schema}).get(item["id"])
                assert semantic is not None
                annotation = semantic.annotation
                schema = semantic.to_wire()
            port = WorkflowInputPort(
                id=item["id"], name=item["name"], kind=item["kind"],
                annotation=annotation, schema=schema,
                default=deserialize_constant(item["default"]) if "default" in item else MISSING,
            )
            wf._interface_inputs[port.id] = port
        nodes_by_name = _admission["nodes"]
        incoming = _admission["incoming"]
        deps = _admission["deps"]
        target_by_node = _admission["targets"]
        if errors is not None:
            errors.extend(ValidationError(kind="missing_input", message="Edge references an unknown node.", node=edge["target_node"], edge_id=edge["id"]) for edge in _admission["missing_edges"])

        store = _get_store_path()
        built: dict[str, Node] = {}
        previous = get_active_workflow()
        set_active_workflow(wf)
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
                            errors.append(
                                ValidationError(
                                    kind="missing_input",
                                    message=(
                                        f"Input edge source '{edge['source_node']}' "
                                        "could not be materialized."
                                    ),
                                    node=name,
                                    edge_id=edge["id"],
                                )
                            )
                            continue
                        raise ValueError(
                            f"Input edge source '{edge['source_node']}' could not be materialized."
                        )
                    source = built[edge["source_node"]]
                    if edge["type"] == "column":
                        kwargs[edge["target_input"]] = ColumnRef(
                            source, edge["source_output"]
                        )
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

                nested_failure_reported = False
                try:
                    with wf.capture_errors() as captured:
                        if node_data["type"] == "workflow":
                            child_errors: list[ValidationError] = []
                            try:
                                child = cls._materialize_graph(
                                    node_data["workflow"],
                                    custom_modules=custom_modules,
                                    source_records=source_records,
                                    auto_install=auto_install,
                                    storage_path=storage_path,
                                    partial=partial,
                                    errors=child_errors,
                                    graph_stack=graph_stack,
                                    _admission=_admission["children"][name],
                                )
                            except BindingError:
                                nested_failure_reported = bool(child_errors)
                                raise
                            finally:
                                if errors is not None:
                                    errors.extend(
                                        replace(error, path=(name, *error.path))
                                        for error in child_errors
                                    )
                            child._build_errors = list(child_errors)
                            child_by_id = child._interface_inputs
                            named_bindings: dict[str, Any] = {}
                            for key, value in kwargs.items():
                                port = child_by_id.get(key)
                                if port is None:
                                    raise ValueError(
                                        f"Unknown input port '{key}' on workflow node '{name}'."
                                    )
                                named_bindings[port.name] = value
                            for port_id, value in node_data.get("bindings", {}).items():
                                port = child_by_id.get(port_id)
                                if port is None:
                                    raise ValueError(
                                        f"Unknown constant input port '{port_id}'."
                                    )
                                if port_id in kwargs:
                                    raise ValueError(
                                        f"Workflow input port '{port_id}' has both an edge and a constant binding."
                                    )
                                named_bindings[port.name] = deserialize_constant(value)
                            node = child(
                                name=name,
                                viewer_additions=node_data.get("viewer_additions"),
                                **named_bindings,
                            )
                            node._input_column_binding_edge_ids.update(column_edge_ids)
                            node._input_dataframe_binding_edge_ids.update(
                                dataframe_port_edge_ids
                            )
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
                            for field_name, value in node_data.get(
                                "constants", {}
                            ).items():
                                decoded = deserialize_constant(value)
                                if field_name in incoming_field_names:
                                    raise ValueError(
                                        f"Tool input '{field_name}' has both an edge and a constant binding."
                                    )
                                if isinstance(kwargs.get(field_name), WorkflowInputRef):
                                    fallback_constants[field_name] = decoded
                                else:
                                    kwargs[field_name] = decoded
                            args = [
                                positional[index]
                                for index in range(max(positional, default=-1) + 1)
                            ]
                            if isinstance(instance, DataFrameTool):
                                node = instance(
                                    *args,
                                    name=name,
                                    output_templates=node_data.get("output_templates"),
                                    viewer_additions=node_data.get("viewer_additions"),
                                    **kwargs,
                                )
                                node._arg_edge_ids = [
                                    positional_edge_ids.get(index)
                                    for index in range(len(args))
                                ]
                            else:
                                if args:
                                    raise ValueError(
                                        "Processing tools cannot have positional DataFrame inputs."
                                    )
                                node = instance(
                                    name=name,
                                    output_templates=node_data.get("output_templates"),
                                    viewer_additions=node_data.get("viewer_additions"),
                                    **kwargs,
                                )
                                if "resource_overrides" in node_data:
                                    from bioimageflow.resources import (
                                        NodeResourceOverrides,
                                    )

                                    node.set_resource_overrides(
                                        NodeResourceOverrides.from_dict(
                                            node_data["resource_overrides"]
                                        )
                                    )
                            node._constant_bindings.update(fallback_constants)
                            node._workflow_input_fallback_constants.update(
                                fallback_constants
                            )
                            node._column_binding_edge_ids.update(column_edge_ids)
                    if errors is not None:
                        for error in captured if partial else captured[:1]:
                            edge = next((
                                item for item in incoming[name]
                                if item.get("target_input") == error.field
                            ), None)
                            errors.append(replace(
                                error,
                                edge=(edge["source_node"], name, error.field),
                                edge_id=edge["id"],
                            ) if edge is not None and error.field is not None else error)
                    if captured and not partial:
                        raise ValueError(captured[0].message)
                    node.enabled = node_data.get("enabled", True)
                    built[name] = node
                except Exception as exc:
                    if nested_failure_reported:
                        raise
                    if not partial and not isinstance(exc, BindingError):
                        raise
                    error = ValidationError(
                        kind="unknown_tool"
                        if isinstance(exc, (ImportError, AttributeError))
                        else "construction_failed",
                        message=str(exc),
                        node=name,
                    )
                    if isinstance(exc, BindingError):
                        error = exc.to_validation_error(name, kind="type_mismatch")
                        edge = next((
                            item for item in incoming[name]
                            if item.get("target_input") == error.field
                        ), None)
                        if edge is not None and error.field is not None:
                            error = replace(
                                error,
                                edge=(edge["source_node"], name, error.field),
                                edge_id=edge["id"],
                            )
                    if errors is not None:
                        errors.append(error)
                    if not partial:
                        raise
                    wf._failed_nodes[name] = error
        finally:
            set_active_workflow(previous)

        for item in graph["interface"]["outputs"]:
            source = item["source"]
            if (
                not isinstance(source, dict)
                or set(source) != {"node", "column"}
                or source["node"] not in built
            ):
                if partial and errors is not None:
                    errors.append(
                        ValidationError(
                            kind="missing_input",
                            message="Workflow output references an unknown source.",
                            node=source.get("node")
                            if isinstance(source, dict)
                            else None,
                        )
                    )
                    continue
                raise ValueError("Workflow output references an unknown source.")
            source_node = built[source["node"]]
            from bioimageflow.workflow_node import WorkflowNode

            declaration = _resolve_output_source(source_node, source["column"])
            if declaration is None:
                if isinstance(source_node, WorkflowNode):
                    raise ValueError(
                        "Workflow output references an unknown child output port."
                    )
                raise ValueError(
                    "Workflow output references an unknown tool output column."
                )
            schema = copy.deepcopy(item.get("schema"))
            if schema is None:
                annotation, schema = declaration
            else:
                semantic = ResolvedSchema.from_columns({item["id"]: schema}).get(item["id"])
                assert semantic is not None
                annotation = semantic.annotation
            port = WorkflowOutputPort(
                id=item["id"],
                name=item["name"],
                annotation=annotation,
                schema=schema,
                source_node=source["node"],
                source_output=source["column"],
                viewer_addition=(
                    None
                    if "viewer_addition" not in item
                    else ViewerSpec.from_dict(item["viewer_addition"])
                ),
            )
            if (
                port.id in wf._interface_inputs
                or port.id in wf._interface_outputs
                or any(
                    candidate.name == port.name
                    for candidate in [
                        *wf._interface_inputs.values(),
                        *wf._interface_outputs.values(),
                    ]
                )
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
                pkg,
                pkg_ver,
                node_data["tool_module"],
                node_data["tool_class"],
            )
        else:
            module = importlib.import_module(node_data["tool_module"])
            tool_class = getattr(module, node_data["tool_class"])
        return tool_class()
