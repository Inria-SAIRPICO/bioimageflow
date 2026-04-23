"""Workflow container and progress events."""

import json
import importlib
import threading
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import TYPE_CHECKING

from bioimageflow.node import (
    set_active_workflow,
    get_active_workflow,
    _reset_name_counters,
    _error_collector,
    Node,
)
from bioimageflow.validation import ValidationError

if TYPE_CHECKING:
    from bioimageflow.engine import DefaultEngine, NodeStep, NodePlan

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

    def topological_order(self) -> list[str]:
        """Return node names in dependency order. Raises on cycle.

        Thin wrapper over :func:`bioimageflow.engine.topological_order`.
        If the graph may contain a cycle, call :meth:`validate` first.
        """
        from bioimageflow.engine import topological_order as _topo
        return _topo(self)

    def downstream_of(self, node_name: str) -> set[str]:
        """Return node names transitively downstream of ``node_name``.

        Excludes ``node_name`` itself. Useful for callers (GUIs, external
        schedulers) that need to mark dependents as cache-invalidated
        after a parameter change.
        """
        if node_name not in self._nodes:
            raise KeyError(
                f"Node '{node_name}' not found. Available: {list(self._nodes)}"
            )

        # Build reverse adjacency (edge from upstream → downstream).
        reverse: dict[str, set[str]] = {n: set() for n in self._nodes}
        for n, node in self._nodes.items():
            for up in node._upstream_nodes:
                if up.name in reverse:
                    reverse[up.name].add(n)
            for arg in node._args:
                if isinstance(arg, Node) and arg.name in reverse:
                    reverse[arg.name].add(n)

        visited: set[str] = set()
        queue: list[str] = list(reverse[node_name])
        while queue:
            nxt = queue.pop()
            if nxt in visited:
                continue
            visited.add(nxt)
            queue.extend(reverse.get(nxt, ()))
        return visited

    def plan(self, *, dev_mode: bool = False) -> "dict[str, NodePlan]":
        """Return a per-node cache-status and signature-hash plan.

        Instantiates a non-Wetlands :class:`DefaultEngine` and calls its
        :meth:`plan` — the returned hashes are byte-identical to what
        :meth:`compute` would compute. No tool code runs.
        """
        from bioimageflow.engine import DefaultEngine
        self._dev_mode = dev_mode
        self._discover_graph(list(self._nodes.values()))
        return DefaultEngine(use_wetlands=False).plan(self)

    def validate(self, *, dev_mode: bool = False) -> list[ValidationError]:
        """Return all domain-level problems in this workflow.

        Runs, in order:

        1. Cycle detection (one error per cycle).
        2. Type compatibility on every column binding.
        3. Missing-required-input check for every node.
        4. Pydantic validation of every node's supplied constants.
        5. Recursive validation of sub-workflows (``path`` is prefixed
           with the parent's node name).

        Steps 1–3 are already enforced by ``Node.__init__`` during
        construction; this method exists so GUIs that built the workflow
        via :meth:`collect_errors` / :meth:`from_dict` can re-check after
        the fact. Step 4 (constant Pydantic validation) is *new behavior*
        and only runs here — it is not performed at construction time
        for backward compatibility.

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
        from bioimageflow.sub_workflow import SubWorkflowNode
        from graphlib import CycleError

        errors: list[ValidationError] = []

        # Step 1: cycle detection (doesn't block the rest — we still check parameters).
        try:
            topological_order(self)
        except CycleError as exc:
            cycle = exc.args[1] if len(exc.args) > 1 else []
            errors.append(ValidationError(
                kind="cycle",
                message=f"Cycle detected: {cycle}",
            ))

        for name, node in self._nodes.items():
            if isinstance(node, SubWorkflowNode):
                # Step 2/3 on the SubWorkflowNode's own inputs.
                _validate_sub_workflow_node(node, errors)
                # Step 5: recurse into the internal DAG.
                sub_errors = _validate_sub_workflow_internal(node)
                for e in sub_errors:
                    errors.append(ValidationError(
                        kind=e.kind,
                        message=e.message,
                        node=e.node,
                        field=e.field,
                        edge=e.edge,
                        path=(name, *e.path),
                    ))
                continue

            # Step 2: type compatibility on column bindings.
            for field, col_ref in node._column_bindings.items():
                err = check_type_compat(node, field, col_ref)
                if err is not None:
                    errors.append(err)
                # Column-not-found on bindings recorded at the structural level.
                upstream_outputs = col_ref.node.tool.Outputs
                if upstream_outputs is not None:
                    from bioimageflow.dataframe_tool import Passthrough
                    output_annotations = upstream_outputs._get_all_annotations()
                    if not issubclass(upstream_outputs, Passthrough) and \
                            col_ref.column not in output_annotations:
                        errors.append(ValidationError(
                            kind="column_not_found",
                            message=(
                                f"Column '{col_ref.column}' not found in "
                                f"outputs of node '{col_ref.node.name}'. "
                                f"Available: {list(output_annotations.keys())}"
                            ),
                            node=name,
                            field=field,
                            edge=(col_ref.node.name, name, field),
                        ))

            # Step 3: missing required inputs.
            input_annotations = node.tool.Inputs._get_all_annotations()
            for field_name in input_annotations:
                if field_name in node._column_bindings:
                    continue
                if field_name in node._constant_bindings:
                    continue
                if hasattr(node.tool.Inputs, field_name):
                    continue
                errors.append(ValidationError(
                    kind="missing_input",
                    message=(
                        f"Missing required input '{field_name}' for tool "
                        f"'{type(node.tool).__name__}'."
                    ),
                    node=name,
                    field=field_name,
                ))

            # Step 4: Pydantic validation of supplied constants.
            try:
                param_errors = validate_parameters(
                    type(node.tool), node._constant_bindings, node=name,
                )
            except Exception as exc:  # pragma: no cover — defensive
                param_errors = [ValidationError(
                    kind="construction_failed",
                    message=f"Pydantic validation setup failed: {exc}",
                    node=name,
                )]
            errors.extend(param_errors)

        # Deduplicate + sort for determinism.
        seen: set[tuple[Any, ...]] = set()
        unique: list[ValidationError] = []
        for e in errors:
            key = (e.path, e.node, e.field, e.kind, e.message)
            if key in seen:
                continue
            seen.add(key)
            unique.append(e)
        unique.sort(key=lambda e: (e.path, e.node or "", e.field or "", e.kind))
        return unique

    @contextmanager
    def collect_errors(self) -> Iterator[list[ValidationError]]:
        """Capture node-construction errors as :class:`ValidationError`.

        Usage::

            wf = Workflow()
            with wf, wf.collect_errors() as errors:
                MyTool()(input=upstream["bad_col"])
            # errors: list[ValidationError]

        Nested blocks push their own list; the outer list is restored on
        exit. Disables the "raise on first error" behavior of ``Node``
        construction only for the duration of the block.
        """
        errs: list[ValidationError] = []
        token = _error_collector.set(errs)
        try:
            yield errs
        finally:
            _error_collector.reset(token)

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

    def to_dict(self) -> dict[str, Any]:
        """Serialize the workflow to a JSON-friendly dict.

        Same shape as the JSON produced by :meth:`export`, with no
        filesystem I/O. The inverse of :meth:`from_dict`.
        """
        from bioimageflow.sub_workflow import SubWorkflowNode
        from bioimageflow.tool_loader import get_tool_package_info

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

        return {
            "nodes": nodes_data,
            "edges": edges_data,
            "config": {
                "storage_path": str(self.storage_path),
                "engine": self.engine_type,
                "max_executions": self.max_executions,
                "max_age": self.max_age,
            },
        }

    def export(self, path: str | Path) -> None:
        """Serialize the workflow to a JSON file."""
        path = Path(path)
        data = self.to_dict()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str))

    @classmethod
    def load(cls, path: str | Path) -> "Workflow":
        """Deserialize a workflow from a JSON file.

        Thin wrapper around :meth:`from_dict`. Preserves the original
        behavior: raises on the first error, auto-installs missing
        versioned packages.
        """
        path = Path(path)
        data = json.loads(path.read_text())
        result = cls.from_dict(data)
        assert isinstance(result, Workflow)  # strict mode
        return result

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        collect_errors: bool = False,
        auto_install: bool = True,
        storage_path_override: str | Path | None = None,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        use_wetlands: bool | None = None,
        wetlands_config: dict[str, Any] | None = None,
    ) -> "Workflow | tuple[Workflow, list[ValidationError]]":
        """Reconstruct a Workflow from a serialized dict.

        Parameters
        ----------
        data
            A dict with the shape produced by :meth:`to_dict` /
            :meth:`export`: ``{"nodes": [...], "edges": [...], "config": {...}}``.
        collect_errors
            When False (default), behaves like :meth:`load` and raises on
            the first error. When True, returns ``(workflow, errors)``; the
            workflow is best-effort partially wired and ``errors`` may be
            non-empty.
        auto_install
            When True (default), missing versioned packages are installed
            automatically. When False, missing packages produce an
            ``unknown_tool`` error (collect mode) or raise (strict mode).
        storage_path_override
            Override ``data["config"]["storage_path"]`` without mutating
            the dict. Useful for GUIs that validate a graph against a
            specific cache path.
        on_progress, use_wetlands, wetlands_config
            Passed to :class:`Workflow`. ``None`` means "use the values
            from ``data['config']`` (or defaults)".
        """
        config = data.get("config", {})
        storage_path = storage_path_override if storage_path_override is not None \
            else config.get("storage_path", "./bif_data")

        wf_kwargs: dict[str, Any] = dict(
            storage_path=storage_path,
            engine=config.get("engine", "sequential"),
            max_executions=config.get("max_executions", 0),
            max_age=config.get("max_age"),
        )
        if on_progress is not None:
            wf_kwargs["on_progress"] = on_progress
        if use_wetlands is not None:
            wf_kwargs["use_wetlands"] = use_wetlands
        if wetlands_config is not None:
            wf_kwargs["wetlands_config"] = wetlands_config
        wf = cls(**wf_kwargs)

        errors: list[ValidationError] = []
        if collect_errors:
            result = wf._reconstruct_from_dict(
                data, auto_install=auto_install, errors=errors,
            )
            return result, errors
        wf._reconstruct_from_dict(data, auto_install=auto_install, errors=None)
        return wf

    def _reconstruct_from_dict(
        self,
        data: dict[str, Any],
        *,
        auto_install: bool,
        errors: list[ValidationError] | None,
    ) -> "Workflow":
        """Internal: reconstruct nodes from a serialized dict.

        When ``errors`` is a list, operates in collect mode — tool
        resolution and node construction failures are appended instead of
        raising. Otherwise, raises on the first error.
        """
        from bioimageflow.tool_loader import (
            load_versioned_package,
            resolve_tool_class,
        )
        from bioimageflow.dataframe_tool import DataFrameTool
        from bioimageflow.sub_workflow import SubWorkflow

        store = _get_store_path()
        tool_instances: dict[str, Any] = {}

        # --- Pass 1: resolve tool classes and instantiate ---
        for node_data in data.get("nodes", []):
            name = node_data["name"]
            try:
                tool_instances[name] = self._resolve_tool_instance(
                    node_data, store=store, auto_install=auto_install,
                    load_versioned_package=load_versioned_package,
                    resolve_tool_class=resolve_tool_class,
                )
            except Exception as exc:
                if errors is None:
                    raise
                kind = "unknown_tool" if isinstance(
                    exc, (ImportError, AttributeError, FileNotFoundError, ModuleNotFoundError)
                ) else "construction_failed"
                errors.append(ValidationError(
                    kind=kind, message=str(exc), node=name,
                ))

        # --- Pass 2: build dependency graph and topologically sort names ---
        edge_map: dict[str, list[dict[str, str]]] = {}
        for edge in data.get("edges", []):
            edge_map.setdefault(edge["to"], []).append(edge)

        from graphlib import TopologicalSorter, CycleError

        dep_graph: dict[str, set[str]] = {}
        node_data_by_name: dict[str, dict[str, Any]] = {}
        for node_data in data.get("nodes", []):
            name = node_data["name"]
            node_data_by_name[name] = node_data
            deps: set[str] = set()
            for edge in edge_map.get(name, []):
                deps.add(edge["from"])
            for arg_name in node_data.get("args", []):
                deps.add(arg_name)
            dep_graph[name] = deps

        try:
            build_order = list(TopologicalSorter(dep_graph).static_order())
        except CycleError as exc:
            if errors is None:
                raise
            errors.append(ValidationError(
                kind="cycle",
                message=f"Cycle detected in serialized graph: {exc.args[1]}",
            ))
            build_order = list(dep_graph.keys())

        # --- Pass 3: construct nodes in dependency order ---
        prev_wf = get_active_workflow()
        set_active_workflow(self)
        _reset_name_counters()

        # Activate error collector for collect mode so Node.__init__
        # per-kwarg failures end up in our list.
        token = None
        if errors is not None:
            token = _error_collector.set(errors)

        node_map: dict[str, Node] = {}
        try:
            for name in build_order:
                if name not in node_data_by_name:
                    continue
                node_data = node_data_by_name[name]
                instance = tool_instances.get(name)
                if instance is None:
                    # Tool resolution already failed — skip construction.
                    continue

                kwargs: dict[str, Any] = {}
                positional_args: list[Node] = []
                missing_upstream = False
                for edge in edge_map.get(name, []):
                    upstream_name = edge["from"]
                    upstream_node = node_map.get(upstream_name)
                    if upstream_node is None:
                        if errors is None:
                            raise KeyError(
                                f"Edge references unknown upstream node "
                                f"'{upstream_name}'"
                            )
                        errors.append(ValidationError(
                            kind="missing_input",
                            message=(
                                f"Upstream node '{upstream_name}' not found "
                                f"for edge into '{name}'"
                            ),
                            node=name,
                            field=edge.get("field"),
                            edge=(upstream_name, name, edge.get("field", "")),
                        ))
                        missing_upstream = True
                        continue
                    if edge["field"] == "__positional__":
                        positional_args.append(upstream_node)
                    else:
                        kwargs[edge["field"]] = upstream_node[edge["column"]]

                for field, value in node_data.get("constants", {}).items():
                    kwargs[field] = _deserialize_constant(value)

                try:
                    if isinstance(instance, SubWorkflow):
                        node = instance(name=name, **kwargs)
                    elif isinstance(instance, DataFrameTool):
                        node = instance(*positional_args, name=name, **kwargs)
                    else:
                        node = instance(name=name, **kwargs)
                    node.enabled = node_data.get("enabled", True)
                    node_map[name] = node
                except Exception as exc:
                    if errors is None:
                        raise
                    # missing_upstream already reported; other failures become construction_failed.
                    if not missing_upstream:
                        errors.append(ValidationError(
                            kind="construction_failed",
                            message=str(exc),
                            node=name,
                        ))
        finally:
            if token is not None:
                _error_collector.reset(token)
            set_active_workflow(prev_wf)

        return self

    def _resolve_tool_instance(
        self,
        node_data: dict[str, Any],
        *,
        store: Path,
        auto_install: bool,
        load_versioned_package: Any,
        resolve_tool_class: Any,
    ) -> Any:
        """Resolve and instantiate a tool or sub-workflow from node_data."""
        if node_data.get("type") == "sub_workflow":
            if node_data.get("sub_workflow_type") == "config":
                from bioimageflow.sub_workflow import SubWorkflow as _SW
                return _SW.from_config(node_data["config"])
            pkg = node_data.get("sub_workflow_package")
            pkg_ver = node_data.get("sub_workflow_package_version")
            if pkg and pkg_ver:
                if auto_install:
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
            return sw_class()

        pkg = node_data.get("tool_package")
        pkg_ver = node_data.get("tool_package_version")
        if pkg and pkg_ver:
            if auto_install:
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
        return tool_class()


def _validate_sub_workflow_node(sw_node: Any, errors: list[ValidationError]) -> None:
    """Validate a SubWorkflowNode's own Inputs (missing, type-compat)."""
    from bioimageflow.validation import check_type_compat

    sub_wf = sw_node.sub_workflow
    input_annotations = sub_wf.Inputs._get_all_annotations()

    for field, col_ref in sw_node._input_column_bindings.items():
        # Type compatibility via check_type_compat requires a Node-like with
        # `.tool.Inputs` — SubWorkflowNode exposes Inputs on sub_workflow.
        class _Shim:
            def __init__(self, name: str, inputs_cls: Any, tool_cls_name: str) -> None:
                self.name = name
                self.tool = type("_T", (), {"Inputs": inputs_cls, "__name__": tool_cls_name})()
        shim = _Shim(sw_node.name, sub_wf.Inputs, type(sub_wf).__name__)
        err = check_type_compat(shim, field, col_ref)
        if err is not None:
            errors.append(err)

    for field_name in input_annotations:
        if field_name in sw_node._input_column_bindings:
            continue
        if field_name in sw_node._input_constant_bindings:
            continue
        if hasattr(sub_wf.Inputs, field_name):
            continue
        errors.append(ValidationError(
            kind="missing_input",
            message=(
                f"Missing required input '{field_name}' for sub-workflow "
                f"'{type(sub_wf).__name__}'."
            ),
            node=sw_node.name,
            field=field_name,
        ))


def _validate_sub_workflow_internal(sw_node: Any) -> list[ValidationError]:
    """Validate the internal nodes of a SubWorkflowNode.

    Creates a transient Workflow around the internal nodes and runs
    :meth:`Workflow.validate` on it. The ``path`` prefix is added by the
    caller.
    """
    inner = Workflow()
    # Register proxy + internal nodes without triggering name collisions.
    inner._nodes[sw_node._proxy_node.name] = sw_node._proxy_node
    for inode in sw_node.internal_nodes:
        inner._nodes[inode.name] = inode
    return inner.validate()


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
