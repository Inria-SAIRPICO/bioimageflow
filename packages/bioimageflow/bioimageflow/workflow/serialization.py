"""Focused methods extracted from the workflow façade."""

# Pyright checks the complete contract on Workflow; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from .common import (
    Any,
    MISSING,
    Node,
    Path,
    copy,
    hashlib,
    json,
    serialize_constant,
    zipfile,
)
from .custom_sources import (
    _register_custom_tool_module,
)


class _SerializationMixin:
    def to_dict(self, *, include_custom_tools: bool = False) -> dict[str, Any]:
        """Serialize the strict recursive schema-version-1 graph."""
        sources: list[dict[str, Any]] = copy.deepcopy(
            self._captured_custom_sources or []
        )
        source_ids: set[str] = {record["id"] for record in sources}
        graph = self._graph_to_dict(
            include_custom_tools=include_custom_tools,
            sources=sources,
            source_ids=source_ids,
        )
        if include_custom_tools and sources:
            return {"archive_version": 1, "workflow": graph, "custom_sources": sources}
        return graph

    def _graph_to_dict(
        self,
        *,
        include_custom_tools: bool,
        sources: list[dict[str, Any]],
        source_ids: set[str],
    ) -> dict[str, Any]:
        from bioimageflow.workflow_node import WorkflowNode
        from bioimageflow.tool_loader import get_tool_package_info

        nodes_data: list[dict[str, Any]] = []
        edges_data: list[dict[str, Any]] = []

        def edge_id(material: str, explicit: str | None = None) -> str:
            if explicit:
                return explicit
            return f"edge-{hashlib.sha256(material.encode()).hexdigest()[:20]}"

        for name, node in self._nodes.items():
            if isinstance(node, WorkflowNode):
                node_info = {
                    "name": name,
                    "type": "workflow",
                    "workflow": node.workflow._graph_to_dict(
                        include_custom_tools=include_custom_tools,
                        sources=sources,
                        source_ids=source_ids,
                    ),
                    "bindings": {
                        port_id: serialize_constant(value)
                        for port_id, value in node._input_constant_bindings.items()
                    },
                }
                if not node.enabled:
                    node_info["enabled"] = False
                nodes_data.append(node_info)

                for port_id, col_ref in node._input_column_bindings.items():
                    explicit = node._input_column_binding_edge_ids.get(port_id)
                    edges_data.append(
                        {
                            "type": "column",
                            "id": edge_id(
                                f"column:{col_ref.node.name}:{col_ref.column}:{name}:{port_id}",
                                explicit,
                            ),
                            "source_node": col_ref.node.name,
                            "source_output": col_ref.column,
                            "target_node": name,
                            "target_input": port_id,
                        }
                    )
                for port_id, upstream in node._input_dataframe_bindings.items():
                    explicit = node._input_dataframe_binding_edge_ids.get(port_id)
                    edges_data.append(
                        {
                            "type": "dataframe",
                            "id": edge_id(
                                f"dataframe:{upstream.name}:{name}:{port_id}", explicit
                            ),
                            "source_node": upstream.name,
                            "target_node": name,
                            "target_input": port_id,
                        }
                    )
            else:
                pkg, pkg_ver, canonical_module = get_tool_package_info(node.tool)
                node_info = {
                    "name": name,
                    "type": "tool",
                    "tool_module": canonical_module,
                    "tool_class": type(node.tool).__name__,
                    "tool_package": pkg,
                    "tool_package_version": pkg_ver,
                    "constants": {
                        field: serialize_constant(value)
                        for field, value in node._constant_bindings.items()
                        if field not in node._workflow_input_bindings
                        or field in node._workflow_input_fallback_constants
                    },
                }
                if include_custom_tools:
                    source_id = _register_custom_tool_module(
                        type(node.tool),
                        records=sources,
                        seen_ids=source_ids,
                    )
                    if source_id is not None:
                        node_info["source_module"] = source_id
                if not node.enabled:
                    node_info["enabled"] = False
                if node.output_templates:
                    node_info["output_templates"] = dict(node.output_templates)
                nodes_data.append(node_info)

                for field, col_ref in node._column_bindings.items():
                    if field in node._workflow_input_bindings:
                        continue
                    explicit = node._column_binding_edge_ids.get(field)
                    edges_data.append(
                        {
                            "type": "column",
                            "id": edge_id(
                                f"column:{col_ref.node.name}:{col_ref.column}:{name}:{field}",
                                explicit,
                            ),
                            "source_node": col_ref.node.name,
                            "source_output": col_ref.column,
                            "target_node": name,
                            "target_input": field,
                        }
                    )
                for idx, arg in enumerate(node._args):
                    if (
                        isinstance(arg, Node)
                        and idx not in node._workflow_dataframe_bindings
                    ):
                        explicit = (
                            node._arg_edge_ids[idx]
                            if idx < len(node._arg_edge_ids)
                            else None
                        )
                        edges_data.append(
                            {
                                "type": "dataframe",
                                "id": edge_id(
                                    f"dataframe:{arg.name}:{name}:{idx}", explicit
                                ),
                                "source_node": arg.name,
                                "target_node": name,
                                "target_position": idx,
                            }
                        )

        result = {
            "schema_version": 1,
            "name": self.name,
            "display_name": self.display_name,
            "interface": {
                "inputs": [
                    {
                        "id": port.id,
                        "name": port.name,
                        "kind": port.kind,
                        **(
                            {"schema": copy.deepcopy(port.schema)}
                            if port.schema is not None
                            else {}
                        ),
                        **(
                            {"default": serialize_constant(port.default)}
                            if port.default is not MISSING
                            else {}
                        ),
                        "targets": copy.deepcopy(port.targets),
                    }
                    for port in self._interface_inputs.values()
                ],
                "outputs": [
                    {
                        "id": port.id,
                        "name": port.name,
                        **(
                            {"schema": copy.deepcopy(port.schema)}
                            if port.schema is not None
                            else {}
                        ),
                        "source": {
                            "node": port.source_node,
                            "column": port.source_output,
                        },
                    }
                    for port in self._interface_outputs.values()
                ],
            },
            "nodes": nodes_data,
            "edges": edges_data,
            "config": {
                "engine": self.engine_type,
                "execution": self.execution,
            },
        }
        if self.output_view is not None:
            result["config"]["output_view"] = self.output_view.to_dict()
        return result

    def export(self, path: str | Path) -> None:
        """Serialize the workflow to a JSON file or BioImageFlow zip archive."""
        path = Path(path)
        if path.suffix == ".zip":
            self._export_archive(path)
            return
        data = self.to_dict(include_custom_tools=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str))

    def _export_archive(self, path: Path) -> None:
        data = self.to_dict(include_custom_tools=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("workflow.json", json.dumps(data, indent=2, default=str))
