"""Focused methods extracted from the workflow façade."""

# Pyright checks the complete contract on Workflow; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false, reportCallIssue=false, reportReturnType=false

from __future__ import annotations

from typing import TYPE_CHECKING

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
    _reset_name_counters,
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
        required = {
            "schema_version",
            "name",
            "display_name",
            "interface",
            "nodes",
            "edges",
            "config",
        }
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
        if not isinstance(graph["interface"], dict) or set(graph["interface"]) != {
            "inputs",
            "outputs",
        }:
            raise ValueError(
                "Workflow interface must contain exactly 'inputs' and 'outputs'."
            )
        if not isinstance(graph["nodes"], list) or not isinstance(graph["edges"], list):
            raise ValueError("Workflow nodes and edges must be arrays.")
        if not all(isinstance(items, list) for items in graph["interface"].values()):
            raise ValueError("Workflow interface inputs and outputs must be arrays.")
        config = graph["config"]
        if not isinstance(config, dict) or not set(config) <= {
            "storage_path",
            "engine",
            "execution",
            "output_view",
        }:
            raise ValueError("Unknown workflow config field.")
        wf = cls(
            name=graph["name"],
            display_name=graph["display_name"],
            storage_path=storage_path_override
            or config.get("storage_path", "./bif_data"),
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
            if (
                not isinstance(item, dict)
                or not {"id", "name", "kind", "targets"} <= set(item)
                or not set(item) <= allowed
            ):
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
                default=deserialize_constant(item["default"])
                if "default" in item
                else MISSING,
            )
            if port.id in wf._interface_inputs or any(
                p.name == port.name for p in wf._interface_inputs.values()
            ):
                raise ValueError("Duplicate workflow input ID or name.")
            wf._interface_inputs[port.id] = port
            serialized_targets[port.id] = copy.deepcopy(item["targets"])

        nodes_by_name: dict[str, dict[str, Any]] = {}
        for node_data in graph["nodes"]:
            if not isinstance(node_data, dict) or node_data.get("type") not in {
                "tool",
                "workflow",
            }:
                raise ValueError("Unknown or malformed workflow node variant.")
            name = node_data.get("name")
            if (
                not isinstance(name, str)
                or not name
                or "/" in name
                or name in nodes_by_name
            ):
                raise ValueError(
                    "Node names must be unique, non-empty, and may not contain '/'."
                )
            required_node_fields = (
                {"name", "type", "workflow", "bindings"}
                if node_data["type"] == "workflow"
                else {
                    "name",
                    "type",
                    "tool_module",
                    "tool_class",
                    "tool_package",
                    "tool_package_version",
                    "constants",
                }
            )
            allowed = (
                {"name", "type", "workflow", "bindings", "enabled"}
                if node_data["type"] == "workflow"
                else {
                    "name",
                    "type",
                    "tool_module",
                    "tool_class",
                    "tool_package",
                    "tool_package_version",
                    "source_module",
                    "constants",
                    "output_templates",
                    "enabled",
                }
            )
            if (
                not required_node_fields <= set(node_data)
                or not set(node_data) <= allowed
            ):
                raise ValueError(f"Malformed or unknown fields on node '{name}'.")
            nodes_by_name[name] = node_data

        incoming: dict[str, list[dict[str, Any]]] = {name: [] for name in nodes_by_name}
        deps: dict[str, set[str]] = {name: set() for name in nodes_by_name}
        edge_ids: set[str] = set()
        for edge in graph["edges"]:
            if not isinstance(edge, dict) or edge.get("type") not in {
                "column",
                "dataframe",
            }:
                raise ValueError("Unknown or malformed edge variant.")
            common = {"type", "id", "source_node", "target_node"}
            allowed = common | (
                {"source_output", "target_input"}
                if edge["type"] == "column"
                else {"target_position", "target_input"}
            )
            if not set(edge) <= allowed or not common <= set(edge):
                raise ValueError("Malformed edge endpoint combination.")
            if edge["type"] == "column" and set(edge) != common | {
                "source_output",
                "target_input",
            }:
                raise ValueError("Column edges require source_output and target_input.")
            if edge["type"] == "dataframe" and (
                ("target_position" in edge) == ("target_input" in edge)
            ):
                raise ValueError(
                    "DataFrame edges target exactly one position or workflow input."
                )
            if edge["id"] in edge_ids:
                raise ValueError(f"Duplicate edge ID '{edge['id']}'.")
            edge_ids.add(edge["id"])
            if (
                edge["source_node"] not in nodes_by_name
                or edge["target_node"] not in nodes_by_name
            ):
                if partial and errors is not None:
                    errors.append(
                        ValidationError(
                            kind="missing_input",
                            message="Edge references an unknown node.",
                            node=edge.get("target_node"),
                            edge_id=edge.get("id"),
                        )
                    )
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
                    raise ValueError(
                        "Workflow interface target references an unknown node."
                    )
                port_endpoint = target["port"]
                if not isinstance(port_endpoint, dict):
                    raise ValueError("Malformed workflow interface target port.")
                endpoint_kind = port_endpoint.get("kind")
                endpoint_fields = {
                    "field": {"kind", "name"},
                    "positional": {"kind", "index"},
                    "workflow": {"kind", "id"},
                }
                if (
                    endpoint_kind not in endpoint_fields
                    or set(port_endpoint) != endpoint_fields[endpoint_kind]
                ):
                    raise ValueError("Malformed workflow interface target port.")
                input_kind = wf._interface_inputs[port_id].kind
                if endpoint_kind != "workflow" and (
                    (input_kind == "field") != (endpoint_kind == "field")
                ):
                    raise ValueError(
                        "Workflow input kind does not match its target port."
                    )
                target_by_node.setdefault(target["node"], []).append(
                    (port_id, target["port"])
                )

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
                            node = child(name=name, **named_bindings)
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
                                    **kwargs,
                                )
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
                        kind="unknown_tool"
                        if isinstance(exc, (ImportError, AttributeError))
                        else "construction_failed",
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
            if (
                not isinstance(item, dict)
                or set(item) - allowed
                or not {"id", "name", "source"} <= set(item)
            ):
                raise ValueError("Malformed workflow output record.")
            if (
                not isinstance(item["id"], str)
                or not item["id"]
                or not isinstance(item["name"], str)
                or not item["name"]
            ):
                raise ValueError("Invalid workflow output ID or name.")
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

            if isinstance(source_node, WorkflowNode):
                if source["column"] not in source_node.workflow._interface_outputs:
                    raise ValueError(
                        "Workflow output references an unknown child output port."
                    )
            else:
                output_schema = source_node.get_output_schema()
                if output_schema is not None and source["column"] not in output_schema:
                    raise ValueError(
                        "Workflow output references an unknown tool output column."
                    )
            port = WorkflowOutputPort(
                id=item["id"],
                name=item["name"],
                annotation=Any,
                schema=copy.deepcopy(item.get("schema")),
                source_node=source["node"],
                source_output=source["column"],
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
