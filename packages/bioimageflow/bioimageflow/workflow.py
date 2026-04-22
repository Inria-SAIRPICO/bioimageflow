"""Workflow container and progress events."""

import json
import importlib
import threading
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import TYPE_CHECKING

from bioimageflow.node import set_active_workflow, get_active_workflow, _reset_name_counters, Node

if TYPE_CHECKING:
    from bioimageflow.engine import DefaultEngine, NodeStep

from bioimageflow_core.environment import EnvironmentSpec
from bioimageflow_core.tool import ProcessingTool


@dataclass
class WorkflowEnvironment:
    """Mutable launch configuration for a Wetlands environment."""
    name: str
    spec: EnvironmentSpec | None = None
    max_workers: int = 0
    worker_env: Callable[[int], dict[str, str]] | None = None
    worker_timeout: float | None = None


def _serialize_constant(value: Any) -> dict[str, Any]:
    """Serialize a constant value with type metadata for lossless round-trip."""
    if isinstance(value, bool):
        return {"__type__": "bool", "value": value}
    if isinstance(value, int):
        return {"__type__": "int", "value": value}
    if isinstance(value, float):
        return {"__type__": "float", "value": value}
    if isinstance(value, (list, tuple)):
        return {"__type__": type(value).__name__, "value": list(value)}
    return {"__type__": "str", "value": str(value)}


def _deserialize_constant(data: Any) -> Any:
    """Deserialize a constant value from its typed representation.

    Supports both the new typed format (``{__type__, value}``) and the
    legacy format (bare string) for backwards compatibility.
    """
    if isinstance(data, dict) and "__type__" in data:
        t = data["__type__"]
        v = data["value"]
        if t == "bool":
            return bool(v)
        if t == "int":
            return int(v)
        if t == "float":
            return float(v)
        if t == "tuple":
            return tuple(v)
        if t == "list":
            return list(v)
        return str(v)
    # Legacy fallback: bare string — try numeric coercion
    if isinstance(data, str):
        try:
            int_val = int(data)
            # Check if it was originally an int (no decimal point)
            if "." not in data:
                return int_val
        except (ValueError, TypeError):
            pass
        try:
            return float(data)
        except (ValueError, TypeError):
            pass
    return data


@dataclass
class ProgressEvent:
    """Progress event reported by the engine."""
    node_name: str
    status: str  # "started", "row_progress", "row_complete", "completed", "cached", "failed", "cancelled"
    row: int = 0
    total_rows: int = 0
    message: str | None = None
    current: int | None = None
    maximum: int | None = None
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
        use_wetlands: bool = True,
        wetlands_config: dict[str, Any] | None = None,
        max_workers: int = 1,
    ) -> None:
        self.storage_path = Path(storage_path)
        self.engine_type = engine
        self.max_executions = max_executions
        self.max_age = max_age
        self.on_progress = on_progress
        self.use_wetlands = use_wetlands
        self.wetlands_config = wetlands_config
        self.max_workers = max_workers
        self._env_configs: dict[str, WorkflowEnvironment] = {}
        self._cancel_event = threading.Event()
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

    def disable(self, *nodes: "Node | str") -> None:
        """Disable nodes by reference or name."""
        for item in nodes:
            node = self._resolve_node(item)
            node.enabled = False

    def enable(self, *nodes: "Node | str") -> None:
        """Enable nodes by reference or name."""
        for item in nodes:
            node = self._resolve_node(item)
            node.enabled = True

    def _resolve_node(self, item: "Node | str") -> Node:
        """Resolve a node reference or name to a Node object."""
        if isinstance(item, str):
            if item not in self._nodes:
                raise KeyError(
                    f"Node '{item}' not found in workflow. "
                    f"Available nodes: {list(self._nodes.keys())}"
                )
            return self._nodes[item]
        return item

    def cancel(self) -> None:
        """Request cancellation of the running workflow."""
        self._cancel_event.set()

    @property
    def cancel_requested(self) -> bool:
        """Whether cancellation has been requested."""
        return self._cancel_event.is_set()

    def get_environment(self, target: "ProcessingTool | EnvironmentSpec | str") -> WorkflowEnvironment:
        """Get the launch configuration proxy for an environment.

        Args:
            target: A ProcessingTool instance, an EnvironmentSpec, or an env name string.

        Returns:
            A shared WorkflowEnvironment proxy. Multiple calls with tools sharing
            the same environment return the same object.
        """
        if isinstance(target, str):
            name = target
            spec = None
        elif isinstance(target, EnvironmentSpec):
            name = target.name
            spec = target
        elif isinstance(target, ProcessingTool):
            name = target.environment.name
            spec = target.environment
        else:
            raise TypeError(
                f"Expected a ProcessingTool, EnvironmentSpec, or str, got {type(target).__name__}"
            )
        if name not in self._env_configs:
            self._env_configs[name] = WorkflowEnvironment(name=name, spec=spec)
        elif spec is not None and self._env_configs[name].spec is None:
            self._env_configs[name].spec = spec
        return self._env_configs[name]

    def compute(self, *targets: Node, dev_mode: bool = False, engine: "DefaultEngine | None" = None) -> Any:
        """Execute the workflow and return results.

        Parameters:
            dev_mode: Development mode flag
            engine: Optional pre-configured engine to use. If None, a default DefaultEngine is created using this workflow's use_wetlands and wetlands_config. Providing an engine allows post-execution inspection and testing."""
        self._cancel_event.clear()
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

        if engine is None:
            from bioimageflow.engine import DefaultEngine
            engine = DefaultEngine(
                use_wetlands=self.use_wetlands,
                wetlands_config=self.wetlands_config,
            )
        results = engine.execute(target_list, self)

        if len(target_list) == 1:
            return list(results.values())[0]
        return results

    def compute_steps(
        self, *targets: Node, dev_mode: bool = False, engine: "DefaultEngine | None" = None
    ) -> "Generator[NodeStep, None, None]":
        """Execute the workflow step by step, yielding a :class:`NodeStep`
        for each node in topological (dependency) order.

        Parameters:
            dev_mode: Development mode flag
            engine: Optional pre-configured engine to use. If None, a default DefaultEngine is created.

        The engine stays alive between yields so Wetlands environments
        remain warm — ideal for interactive debugging.

        Usage::

            for step in wf.compute_steps(results):
                print(f"Next: {step.node_name}")
                step.prepare()     # optional: launches env — attach debugger here
                df = step.execute()
                print(df.head())

        If ``step.execute()`` is not called before advancing to the next
        iteration, the step auto-executes to keep downstream nodes consistent.
        """
        self._dev_mode = dev_mode

        if not targets:
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

        target_list = list(targets)
        self._discover_graph(target_list)

        if engine is None:
            from bioimageflow.engine import DefaultEngine
            engine = DefaultEngine(
                use_wetlands=self.use_wetlands,
                wetlands_config=self.wetlands_config,
            )
        yield from engine.execute_steps(target_list, self)

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
        from bioimageflow.sub_workflow import SubWorkflowNode
        from bioimageflow.tool_loader import get_tool_package_info

        path = Path(path)
        nodes_data: list[dict[str, Any]] = []
        edges_data: list[dict[str, str]] = []

        for name, node in self._nodes.items():
            if isinstance(node, SubWorkflowNode):
                from bioimageflow.sub_workflow import _ConfigDrivenSubWorkflow
                node_info: dict[str, Any]
                if isinstance(node.sub_workflow, _ConfigDrivenSubWorkflow):
                    node_info = {
                        "name": name,
                        "type": "sub_workflow",
                        "sub_workflow_type": "config",
                        "config": node.sub_workflow._config,
                        "constants": {},
                    }
                else:
                    pkg, pkg_ver, canonical_module = get_tool_package_info(
                        node.sub_workflow
                    )
                    node_info = {
                        "name": name,
                        "type": "sub_workflow",
                        "sub_workflow_module": canonical_module,
                        "sub_workflow_class": type(node.sub_workflow).__name__,
                        "sub_workflow_package": pkg,
                        "sub_workflow_package_version": pkg_ver,
                        "constants": {},
                    }
                if not node.enabled:
                    node_info["enabled"] = False
                for field, value in node._input_constant_bindings.items():
                    node_info["constants"][field] = _serialize_constant(value)
                nodes_data.append(node_info)

                # Input column binding edges
                for field, col_ref in node._input_column_bindings.items():
                    edges_data.append({
                        "from": col_ref.node.name,
                        "to": name,
                        "column": col_ref.column,
                        "field": field,
                    })
            else:
                pkg, pkg_ver, canonical_module = get_tool_package_info(node.tool)
                node_info = {
                    "name": name,
                    "tool_module": canonical_module,
                    "tool_class": type(node.tool).__name__,
                    "tool_package": pkg,
                    "tool_package_version": pkg_ver,
                    "constants": {},
                    "args": [arg.name for arg in node._args if isinstance(arg, Node)],
                }
                if not node.enabled:
                    node_info["enabled"] = False
                for field, value in node._constant_bindings.items():
                    node_info["constants"][field] = _serialize_constant(value)
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

        # First pass: create tool/sub-workflow instances
        from bioimageflow.tool_loader import (
            load_versioned_package, resolve_tool_class,
        )
        store = _get_store_path()

        for node_data in data["nodes"]:
            if node_data.get("type") == "sub_workflow":
                if node_data.get("sub_workflow_type") == "config":
                    from bioimageflow.sub_workflow import SubWorkflow as _SW
                    tool_instances[node_data["name"]] = _SW.from_config(
                        node_data["config"]
                    )
                else:
                    pkg = node_data.get("sub_workflow_package")
                    pkg_ver = node_data.get("sub_workflow_package_version")
                    if pkg and pkg_ver:
                        _auto_install_if_missing(pkg, pkg_ver, store)
                        load_versioned_package(pkg, pkg_ver, store)
                        sw_class = resolve_tool_class(
                            pkg, pkg_ver,
                            node_data["sub_workflow_module"],
                            node_data["sub_workflow_class"],
                        )
                    else:
                        module = importlib.import_module(node_data["sub_workflow_module"])
                        sw_class = getattr(module, node_data["sub_workflow_class"])
                    tool_instances[node_data["name"]] = sw_class()
            else:
                pkg = node_data.get("tool_package")
                pkg_ver = node_data.get("tool_package_version")
                if pkg and pkg_ver:
                    _auto_install_if_missing(pkg, pkg_ver, store)
                    load_versioned_package(pkg, pkg_ver, store)
                    tool_class = resolve_tool_class(
                        pkg, pkg_ver,
                        node_data["tool_module"],
                        node_data["tool_class"],
                    )
                else:
                    module = importlib.import_module(node_data["tool_module"])
                    tool_class = getattr(module, node_data["tool_class"])
                tool_instances[node_data["name"]] = tool_class()

        # Build nodes in dependency order

        # Build edge lookup
        edge_map: dict[str, list[dict[str, str]]] = {}
        for edge in data["edges"]:
            edge_map.setdefault(edge["to"], []).append(edge)

        # Build dependency graph for O(V+E) topological sort
        from graphlib import TopologicalSorter

        dep_graph: dict[str, set[str]] = {}
        node_data_by_name: dict[str, dict[str, Any]] = {}
        for node_data in data["nodes"]:
            name = node_data["name"]
            node_data_by_name[name] = node_data
            deps: set[str] = set()
            for edge in edge_map.get(name, []):
                deps.add(edge["from"])
            for arg_name in node_data.get("args", []):
                deps.add(arg_name)
            dep_graph[name] = deps

        build_order = list(TopologicalSorter(dep_graph).static_order())

        prev_wf = get_active_workflow()
        set_active_workflow(wf)
        _reset_name_counters()

        try:
            from bioimageflow.dataframe_tool import DataFrameTool
            from bioimageflow.sub_workflow import SubWorkflow

            for name in build_order:
                node_data = node_data_by_name[name]
                instance = tool_instances[name]
                kwargs: dict[str, Any] = {}
                positional_args: list[Node] = []

                for edge in edge_map.get(name, []):
                    if edge["field"] == "__positional__":
                        positional_args.append(node_map[edge["from"]])
                    else:
                        kwargs[edge["field"]] = node_map[edge["from"]][edge["column"]]

                # Add constants with type recovery
                for field, value in node_data.get("constants", {}).items():
                    kwargs[field] = _deserialize_constant(value)

                if isinstance(instance, SubWorkflow):
                    node = instance(name=name, **kwargs)
                elif isinstance(instance, DataFrameTool):
                    node = instance(*positional_args, name=name, **kwargs)
                else:
                    node = instance(name=name, **kwargs)

                node.enabled = node_data.get("enabled", True)
                node_map[name] = node
        finally:
            set_active_workflow(prev_wf)

        return wf


def _get_store_path() -> Path:
    from bioimageflow.tool_loader import _get_tool_store_path
    return _get_tool_store_path()


def _auto_install_if_missing(pkg: str, pkg_ver: str, store: Path) -> None:
    """Install a versioned package into the tool store if missing."""
    pkg_dir = store / pkg / pkg_ver / pkg
    if pkg_dir.exists():
        return
    from bioimageflow.tool_loader import ensure_installed
    # Use the module name as the PyPI name (hyphens for underscores)
    pypi_name = pkg.replace("_", "-")
    ensure_installed(pkg, pkg_ver, pypi_name, store)
