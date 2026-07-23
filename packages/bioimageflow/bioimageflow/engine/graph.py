"""Focused methods extracted from the execution engine."""

from __future__ import annotations

from .common import (
    Any,
    CycleError,
    EnvironmentMismatchError,
    Node,
    ProcessingTool,
    TopologicalSorter,
)


class _GraphMixin:
    def _collect_reachable(self, node: Node, visited: set[Node]) -> None:
        """Collect all nodes reachable from target (upstream)."""
        if node in visited:
            return
        visited.add(node)
        for upstream in node._upstream_nodes:
            self._collect_reachable(upstream, visited)
        for arg in node._args:
            if isinstance(arg, Node):
                self._collect_reachable(arg, visited)

    def _compile_execution_graph(
        self,
        targets: list[Node],
    ) -> tuple[set[Node], dict[Node, set[Node]], dict[Node, str]]:
        """Flatten recursive workflow invocations into one execution graph."""
        from bioimageflow.workflow_node import WorkflowNode

        nodes: set[Node] = set()
        extra_dependencies: dict[Node, set[Node]] = {}
        scoped_names: dict[Node, str] = {}
        expanded: set[WorkflowNode] = set()

        def visit(node: Node) -> None:
            if node not in nodes:
                nodes.add(node)
                for upstream in node._upstream_nodes:
                    visit(upstream)
                for argument in node._args:
                    if isinstance(argument, Node):
                        visit(argument)
            if isinstance(node, WorkflowNode) and node not in expanded:
                prefix = "" if node._is_root_boundary else node.name
                expand(node, prefix)

        def expand(boundary: WorkflowNode, prefix: str) -> None:
            expanded.add(boundary)
            if not boundary.enabled:
                return
            immediate = list(boundary.internal_nodes)
            immediate_set = set(immediate)
            gates = set(boundary._upstream_nodes)
            gates.update(
                argument for argument in boundary._args if isinstance(argument, Node)
            )
            used: set[Node] = set()
            for internal in immediate:
                path = f"{prefix}/{internal._name}" if prefix else internal._name
                scoped_names[internal] = path
                nodes.add(internal)
                extra_dependencies.setdefault(internal, set()).update(gates)
                for upstream in internal._upstream_nodes:
                    if upstream in immediate_set:
                        used.add(upstream)
                    else:
                        visit(upstream)
                for argument in internal._args:
                    if isinstance(argument, Node):
                        if argument in immediate_set:
                            used.add(argument)
                        else:
                            visit(argument)
            for internal in immediate:
                if isinstance(internal, WorkflowNode) and internal not in expanded:
                    expand(internal, scoped_names[internal])
            terminals = {
                internal
                for internal in immediate
                if internal.enabled and internal not in used
            }
            published_sources = {
                reference.node for reference in boundary._published_outputs.values()
            }
            extra_dependencies.setdefault(boundary, set()).update(
                terminals | published_sources
            )

        for target in targets:
            visit(target)
        return nodes, extra_dependencies, scoped_names

    def _filter_executable(
        self,
        order: list[Node],
        extra_dependencies: dict[Node, set[Node]] | None = None,
    ) -> tuple[list[Node], set[Node]]:
        """Remove disabled nodes and nodes with disabled upstreams.

        Returns (executable_nodes, skipped_nodes). Walks in topological order
        so that by the time we visit a node, all its upstreams are classified.
        """
        skipped: set[Node] = set()
        executable: list[Node] = []
        for node in order:
            if not node.enabled:
                skipped.add(node)
                continue
            upstream_skipped = any(up in skipped for up in node._upstream_nodes) or any(
                arg in skipped for arg in node._args if isinstance(arg, Node)
            )
            if extra_dependencies is not None:
                upstream_skipped = upstream_skipped or any(
                    dependency in skipped
                    for dependency in extra_dependencies.get(node, ())
                )
            if upstream_skipped:
                skipped.add(node)
                continue
            executable.append(node)
        return executable, skipped

    def _check_env_mismatches(self, nodes: set[Node]) -> None:
        """Check for environment name conflicts with different dependencies."""
        from bioimageflow.workflow_node import WorkflowNode

        expanded: set[Node] = set()

        def collect(candidates: set[Node]) -> None:
            for candidate in candidates:
                if candidate in expanded:
                    continue
                expanded.add(candidate)
                if isinstance(candidate, WorkflowNode) and candidate.enabled:
                    collect(set(candidate.internal_nodes))

        collect(nodes)
        env_specs: dict[str, tuple[Any, str]] = {}  # name -> (env, tool_name)
        for node in expanded:
            if isinstance(node.tool, ProcessingTool) and hasattr(
                node.tool, "environment"
            ):
                env = node.tool.environment
                if env.name in env_specs:
                    existing_env, existing_tool = env_specs[env.name]
                    if existing_env.dependencies != env.dependencies:
                        raise EnvironmentMismatchError(
                            f"Environment mismatch for '{env.name}': "
                            f"tool '{existing_tool}' requires {existing_env.dependencies}, "
                            f"but tool '{type(node.tool).__name__}' requires {env.dependencies}."
                        )
                else:
                    env_specs[env.name] = (env, type(node.tool).__name__)

    def _topological_sort(
        self,
        nodes: set[Node],
        extra_dependencies: dict[Node, set[Node]] | None = None,
    ) -> list[Node]:
        """Topological sort of reachable nodes using graphlib.TopologicalSorter."""
        dep_graph = self._build_dep_graph_from_set(nodes, extra_dependencies)
        try:
            return list(TopologicalSorter(dep_graph).static_order())
        except CycleError as exc:
            raise RuntimeError(
                f"Cycle detected in the DAG. The workflow graph must be acyclic. "
                f"Cycle info: {exc.args[1]}"
            ) from exc

    def _build_dep_graph_from_set(
        self,
        nodes: set[Node],
        extra_dependencies: dict[Node, set[Node]] | None = None,
    ) -> dict[Node, set[Node]]:
        """Build dependency graph from a set of nodes (for topological sort)."""
        dep_graph: dict[Node, set[Node]] = {}
        for node in nodes:
            all_upstream: set[Node] = set(node._upstream_nodes)
            for arg in node._args:
                if isinstance(arg, Node):
                    all_upstream.add(arg)
            if extra_dependencies is not None:
                all_upstream.update(extra_dependencies.get(node, ()))
            dep_graph[node] = {up for up in all_upstream if up in nodes}
        return dep_graph

    # ── Cache pre-check ─────────────────────────────────────────────────
