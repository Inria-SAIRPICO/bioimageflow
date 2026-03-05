"""Workflow container and progress events."""

import json
import importlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bioimageflow.node import set_active_workflow, get_active_workflow, _reset_name_counters, Node


@dataclass
class ProgressEvent:
    """Progress event reported by the engine."""
    node_name: str
    status: str
    row: int = 0
    total_rows: int = 0
    timestamp: float = 0.0


class Workflow:
    """Holds the DAG and provides configuration for execution."""

    def __init__(
        self,
        storage_path: str | Path = "./bif_data",
        engine: str = "sequential",
        max_executions: int = 0,
        max_age: str | None = None,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> None:
        self.storage_path = Path(storage_path)
        self.engine_type = engine
        self.max_executions = max_executions
        self.max_age = max_age
        self.on_progress = on_progress
        self._nodes: dict[str, Node] = {}
        self._prev_workflow: Any = None
        self._dev_mode: bool = False

    def __enter__(self) -> "Workflow":
        self._prev_workflow = get_active_workflow()
        set_active_workflow(self)
        _reset_name_counters()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        set_active_workflow(self._prev_workflow)
        return False

    def _register_node(self, node: Node) -> None:
        """Register a node with this workflow."""
        self._nodes[node.name] = node

    @property
    def nodes(self) -> dict[str, Node]:
        return dict(self._nodes)

    def compute(self, *targets: Node, dev_mode: bool = False) -> Any:
        """Execute the workflow and return results."""
        self._dev_mode = dev_mode

        if not targets:
            # Auto-detect terminal nodes
            all_upstream: set[str] = set()
            for node in self._nodes.values():
                for up in node._upstream_nodes:
                    all_upstream.add(up.name)
                for arg in node._args:
                    if isinstance(arg, Node):
                        all_upstream.add(arg.name)
            terminals = [
                n for name, n in self._nodes.items()
                if name not in all_upstream
            ]
            if not terminals:
                terminals = list(self._nodes.values())
            targets = tuple(terminals)

        # If targets are not registered (explicit workflow without context manager),
        # discover the graph by tracing upstream
        target_list = list(targets)
        self._discover_graph(target_list)

        from bioimageflow.engine import SequentialEngine
        engine = SequentialEngine()
        results = engine.execute(target_list, self)

        if len(target_list) == 1:
            return list(results.values())[0]
        return results

    def _discover_graph(self, targets: list[Node]) -> None:
        """Discover and register all nodes reachable from targets."""
        visited: set[str] = set()
        queue = list(targets)
        while queue:
            node = queue.pop(0)
            if node.name in visited:
                continue
            visited.add(node.name)
            if node.name not in self._nodes:
                self._nodes[node.name] = node
            for up in node._upstream_nodes:
                queue.append(up)
            for arg in node._args:
                if isinstance(arg, Node):
                    queue.append(arg)

    def export(self, path: str | Path) -> None:
        """Serialize the workflow to JSON."""
        path = Path(path)
        nodes_data: list[dict[str, Any]] = []
        edges_data: list[dict[str, str]] = []

        for name, node in self._nodes.items():
            node_info: dict[str, Any] = {
                "name": name,
                "tool_module": type(node.tool).__module__,
                "tool_class": type(node.tool).__name__,
                "constants": {},
                "args": [arg.name for arg in node._args if isinstance(arg, Node)],
            }
            for field, value in node._constant_bindings.items():
                node_info["constants"][field] = str(value)
            nodes_data.append(node_info)

            for field, col_ref in node._column_bindings.items():
                edges_data.append({
                    "from": col_ref.node.name,
                    "to": name,
                    "column": col_ref.column,
                    "field": field,
                })
            for arg in node._args:
                if isinstance(arg, Node):
                    edges_data.append({
                        "from": arg.name,
                        "to": name,
                        "column": "__positional__",
                        "field": "__positional__",
                    })

        data: dict[str, Any] = {
            "nodes": nodes_data,
            "edges": edges_data,
            "config": {
                "storage_path": str(self.storage_path),
                "engine": self.engine_type,
                "max_executions": self.max_executions,
                "max_age": self.max_age,
            },
        }

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str))

    @classmethod
    def load(cls, path: str | Path) -> "Workflow":
        """Deserialize a workflow from JSON."""
        path = Path(path)
        data = json.loads(path.read_text())

        config = data["config"]
        wf = cls(
            storage_path=config["storage_path"],
            engine=config.get("engine", "sequential"),
            max_executions=config.get("max_executions", 0),
            max_age=config.get("max_age"),
        )

        # Reconstruct nodes
        node_map: dict[str, Node] = {}
        tool_instances: dict[str, Any] = {}

        # First pass: create tool instances
        for node_data in data["nodes"]:
            module = importlib.import_module(node_data["tool_module"])
            tool_class = getattr(module, node_data["tool_class"])
            tool = tool_class()
            tool_instances[node_data["name"]] = tool

        # Build nodes in dependency order
        built: set[str] = set()
        remaining: list[dict[str, Any]] = list(data["nodes"])

        # Build edge lookup
        edge_map: dict[str, list[dict[str, str]]] = {}
        for edge in data["edges"]:
            edge_map.setdefault(edge["to"], []).append(edge)

        prev_wf = get_active_workflow()
        set_active_workflow(wf)
        _reset_name_counters()

        try:
            max_iterations = len(remaining) * len(remaining) + 1
            iteration = 0
            while remaining and iteration < max_iterations:
                iteration += 1
                for node_data in list(remaining):
                    name = node_data["name"]
                    # Check if all dependencies are built
                    deps_ready = True
                    edges = edge_map.get(name, [])
                    for edge in edges:
                        if edge["from"] not in built:
                            deps_ready = False
                            break
                    # Also check positional args
                    for arg_name in node_data.get("args", []):
                        if arg_name not in built:
                            deps_ready = False
                            break

                    if not deps_ready:
                        continue

                    tool = tool_instances[name]
                    kwargs: dict[str, Any] = {}
                    positional_args: list[Node] = []

                    for edge in edges:
                        if edge["field"] == "__positional__":
                            positional_args.append(node_map[edge["from"]])
                        else:
                            kwargs[edge["field"]] = node_map[edge["from"]][edge["column"]]

                    # Add constants
                    for field, value in node_data.get("constants", {}).items():
                        # Try to convert to appropriate type
                        try:
                            kwargs[field] = float(value)
                        except (ValueError, TypeError):
                            kwargs[field] = value

                    from bioimageflow.dataframe_tool import DataFrameTool
                    if isinstance(tool, DataFrameTool):
                        node = tool(*positional_args, name=name, **kwargs)
                    else:
                        node = tool(name=name, **kwargs)

                    node_map[name] = node
                    built.add(name)
                    remaining.remove(node_data)
        finally:
            set_active_workflow(prev_wf)

        return wf
