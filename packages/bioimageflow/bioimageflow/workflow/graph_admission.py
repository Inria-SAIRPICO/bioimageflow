"""Pure whole-graph admission before tool resolution or materialization."""
from copy import deepcopy
from typing import Any

from bioimageflow.validation.constants import deserialize_constant
from bioimageflow.validation.resolved import ResolvedSchema
from bioimageflow_core.viewer import ViewerSpec


def admit_graph(graph: dict[str, Any], *, partial: bool = False, active: tuple[int, ...] = ()) -> dict[str, Any]:
    if id(graph) in active:
        raise ValueError("Recursive workflow graph containment is not allowed.")
    active = (*active, id(graph))
    required = {"schema_version", "name", "display_name", "interface", "nodes", "edges", "config"}
    if not isinstance(graph, dict) or set(graph) != required:
        raise ValueError(f"Workflow graph fields must be exactly {sorted(required)}.")
    if graph["schema_version"] != 2 or isinstance(graph["schema_version"], bool):
        raise ValueError("Only current workflow schema_version 2 is supported.")
    if not isinstance(graph["name"], str) or not graph["name"] or "/" in graph["name"] or not isinstance(graph["display_name"], str):
        raise ValueError("Invalid workflow definition metadata.")
    interface = graph["interface"]
    if not isinstance(interface, dict) or set(interface) != {"inputs", "outputs"} or not all(isinstance(value, list) for value in interface.values()):
        raise ValueError("Workflow interface must contain exactly inputs and outputs arrays.")
    if not isinstance(graph["nodes"], list) or not isinstance(graph["edges"], list):
        raise ValueError("Workflow nodes and edges must be arrays.")
    if not isinstance(graph["config"], dict) or not set(graph["config"]) <= {"engine", "execution", "output_view"}:
        raise ValueError("Unknown workflow config field.")
    nodes: dict[str, Any] = {}
    children: dict[str, Any] = {}
    for record in graph["nodes"]:
        if not isinstance(record, dict) or record.get("type") not in {"tool", "workflow"}:
            raise ValueError("Unknown or malformed workflow node variant.")
        name = record.get("name")
        if not isinstance(name, str) or not name or "/" in name or name in nodes:
            raise ValueError("Node names must be unique, non-empty, and may not contain '/'.")
        common = {"name", "type"}
        fields = common | ({"workflow", "bindings"} if record["type"] == "workflow" else {"tool_module", "tool_class", "tool_package", "tool_package_version", "constants"})
        optional = {"enabled", "viewer_additions"} | (set() if record["type"] == "workflow" else {"source_module", "output_templates", "resource_overrides"})
        if not fields <= set(record) or not set(record) <= fields | optional:
            raise ValueError(f"Malformed or unknown fields on node '{name}'.")
        if "enabled" in record and not isinstance(record["enabled"], bool):
            raise ValueError("Node enabled state must be a bool.")
        additions = record.get("viewer_additions", {})
        if not isinstance(additions, dict) or not all(isinstance(key, str) and key for key in additions):
            raise ValueError("Viewer additions must have named output keys.")
        for value in additions.values():
            ViewerSpec.from_dict(value)
        bindings = record["bindings" if record["type"] == "workflow" else "constants"]
        if not isinstance(bindings, dict) or not all(isinstance(key, str) and key for key in bindings):
            raise ValueError("Constant bindings must have named input keys.")
        for value in bindings.values():
            deserialize_constant(value)
        if record["type"] == "workflow":
            children[name] = admit_graph(record["workflow"], partial=partial, active=active)
        nodes[name] = record
    incoming: dict[str, list[Any]] = {name: [] for name in nodes}
    deps: dict[str, set[str]] = {name: set() for name in nodes}
    edge_ids: set[str] = set()
    endpoints: set[tuple[str, str, Any]] = set()
    missing_edges = []
    for edge in graph["edges"]:
        if not isinstance(edge, dict) or edge.get("type") not in {"column", "dataframe"}:
            raise ValueError("Unknown or malformed edge variant.")
        common = {"type", "id", "source_node", "target_node"}
        if edge["type"] == "column":
            required_edge = common | {"source_output", "target_input"}
        else:
            if ("target_input" in edge) == ("target_position" in edge):
                raise ValueError("DataFrame edges target exactly one position or workflow input.")
            required_edge = common | ({"target_input"} if "target_input" in edge else {"target_position"})
        if set(edge) != required_edge:
            raise ValueError("Malformed edge endpoint combination.")
        for key in required_edge - {"target_position"}:
            if not isinstance(edge[key], str) or not edge[key]:
                raise ValueError("Edge identifiers and field endpoints must be non-empty strings.")
        if "target_position" in edge and (type(edge["target_position"]) is not int or edge["target_position"] < 0):
            raise ValueError("DataFrame positions must be nonnegative integers.")
        endpoint = (edge["target_node"], "position" if "target_position" in edge else "field", edge.get("target_position", edge.get("target_input")))
        if edge["id"] in edge_ids or endpoint in endpoints:
            raise ValueError("Duplicate edge ID or target endpoint.")
        edge_ids.add(edge["id"])
        endpoints.add(endpoint)
        target = nodes.get(edge["target_node"])
        if target is not None and "target_input" in edge and edge["target_input"] in target["bindings" if target["type"] == "workflow" else "constants"]:
            raise ValueError("An input cannot have both an edge and a constant binding.")
        if edge["source_node"] not in nodes or target is None:
            if not partial:
                raise ValueError("Edge references an unknown node.")
            missing_edges.append(edge)
            continue
        incoming[edge["target_node"]].append(edge)
        deps[edge["target_node"]].add(edge["source_node"])
    ids: set[str] = set()
    names: set[str] = set()
    targets: dict[str, list[tuple[str, Any]]] = {}
    for item in [*interface["inputs"], *interface["outputs"]]:
        if not isinstance(item, dict):
            raise ValueError("Malformed workflow interface record.")
        for key, seen in (("id", ids), ("name", names)):
            if not isinstance(item.get(key), str) or not item[key] or item[key] in seen:
                raise ValueError("Duplicate or invalid workflow interface ID or name.")
            seen.add(item[key])
    published: set[tuple[str, str, Any]] = set()
    for item in interface["inputs"]:
        if not {"id", "name", "kind", "targets"} <= set(item) or not set(item) <= {"id", "name", "kind", "targets", "schema", "default"} or item["kind"] not in {"field", "dataframe"} or item["name"] == "name" or not isinstance(item["targets"], list):
            raise ValueError("Malformed workflow input record.")
        if item["kind"] == "field" and item.get("schema") is not None:
            ResolvedSchema.from_columns({item["name"]: item["schema"]})
        if "default" in item:
            deserialize_constant(item["default"])
        for target in item["targets"]:
            if not isinstance(target, dict) or set(target) != {"node", "port"} or target["node"] not in nodes:
                raise ValueError("Workflow interface target references an unknown node.")
            port = target["port"]
            fields = {"field": {"kind", "name"}, "positional": {"kind", "index"}, "workflow": {"kind", "id"}}
            if not isinstance(port, dict) or port.get("kind") not in fields or set(port) != fields[port["kind"]]:
                raise ValueError("Malformed workflow interface target port.")
            kind = port["kind"]
            key = port.get("name", port.get("id", port.get("index")))
            if kind == "positional" and (type(key) is not int or key < 0) or kind != "positional" and (not isinstance(key, str) or not key):
                raise ValueError("Malformed workflow interface target endpoint.")
            if kind != "workflow" and ((item["kind"] == "field") != (kind == "field")):
                raise ValueError("Workflow input kind does not match its target port.")
            endpoint = (target["node"], "position" if kind == "positional" else "field", key)
            if endpoint in endpoints or endpoint in published:
                raise ValueError("A workflow interface target cannot shadow another target or internal edge.")
            published.add(endpoint)
            targets.setdefault(target["node"], []).append((item["id"], port))
    for item in interface["outputs"]:
        if not {"id", "name", "source"} <= set(item) or not set(item) <= {"id", "name", "source", "schema", "viewer_addition"}:
            raise ValueError("Malformed workflow output record.")
        if item.get("schema") is not None:
            ResolvedSchema.from_columns({item["name"]: item["schema"]})
        source = item["source"]
        if not isinstance(source, dict) or set(source) != {"node", "column"} or not all(isinstance(value, str) and value for value in source.values()):
            raise ValueError("Malformed workflow output source.")
        if not partial and source["node"] not in nodes:
            raise ValueError("Workflow output references an unknown source.")
        if "viewer_addition" in item:
            ViewerSpec.from_dict(item["viewer_addition"])
    return {"nodes": nodes, "incoming": incoming, "deps": deps, "targets": targets, "missing_edges": missing_edges, "children": children}


def capture_graph(graph: dict[str, Any], *, partial: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """Capture the whole manifest before trusted constructors can see aliases."""
    captured = deepcopy(graph)
    return captured, admit_graph(captured, partial=partial)
