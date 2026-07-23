"""Focused methods extracted from the workflow façade."""

# Pyright checks the complete contract on Workflow; this module contains one partial mixin.
# pyright: reportArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from .common import (
    Any,
    InvalidatedSelection,
    Node,
    _absolute_runtime_path,
    _clear_currents_for_node,
    _remove_current_selection,
)

if TYPE_CHECKING:
    from bioimageflow.engine import NodePlan
    from .model import Workflow


class _InvalidationMixin:
    def invalidate(
        self,
        node_ids: "Iterable[str]",
        *,
        cascade: bool = True,
    ) -> set[InvalidatedSelection]:
        """Remove cache selections for the given nodes.

        Returns the selections whose ``current.json`` pointers were
        removed. ``cascade=True`` (the default) also removes selections
        for every node transitively downstream of each input node, so a
        subsequent run recomputes or reselects everything that depended on
        the changed node. Immutable ``records/<record-id>/`` directories
        are retained.

        ``KeyError`` is raised if any name in ``node_ids`` is not
        registered with this workflow — matching the existing behavior
        of :meth:`downstream_of`.

        Concurrency
        -----------
        This method is **not** safe to call concurrently with
        :meth:`compute` on the same workflow. The library does not
        currently expose a public lock primitive; callers that need
        to invalidate while a compute is in flight must coordinate
        externally (e.g., cancel + join + invalidate).
        """
        from bioimageflow.dataframe_tool import DataFrameTool
        from bioimageflow.engine import DefaultEngine
        from bioimageflow.storage import Storage
        from bioimageflow.workflow_node import WorkflowNode
        from bioimageflow_core.tool import ProcessingTool

        scoped_nodes: dict[str, Node] = {}

        def collect(definition: Workflow, prefix: str = "") -> None:
            for local_name, candidate in definition._nodes.items():
                path = f"{prefix}/{local_name}" if prefix else local_name
                scoped_nodes[path] = candidate
                if isinstance(candidate, WorkflowNode):
                    collect(candidate.workflow, path)

        collect(self)
        targets: set[str] = set()
        for nid in node_ids:
            if nid not in scoped_nodes:
                raise KeyError(
                    f"Node '{nid}' not found. Available: {list(scoped_nodes)}"
                )
            targets.add(nid)
            if isinstance(scoped_nodes[nid], WorkflowNode):
                targets.update(
                    path for path in scoped_nodes if path.startswith(f"{nid}/")
                )

        invalidated: set[InvalidatedSelection] = set()
        self._discover_graph(list(self._nodes.values()))
        engine = DefaultEngine(use_wetlands=False)
        try:
            plan = engine.plan(self)
        except Exception as exc:
            from bioimageflow.storage import CacheCorruptionError

            if not isinstance(exc, CacheCorruptionError):
                raise
            plan = {}
        if cascade:
            downstream: dict[str, set[str]] = {name: set() for name in plan}
            for name, entry in plan.items():
                for upstream in entry.upstream:
                    downstream.setdefault(upstream, set()).add(name)
            queue = list(targets)
            while queue:
                current = queue.pop()
                for dependent in downstream.get(current, ()):
                    if dependent not in targets:
                        targets.add(dependent)
                        queue.append(dependent)

        storage = Storage(self.storage_path)
        for name in targets:
            node = scoped_nodes[name]
            if isinstance(node, WorkflowNode):
                continue
            entry = plan.get(name)
            result_key = entry.final_result_key if entry is not None else None
            if isinstance(node.tool, DataFrameTool):
                if result_key is not None:
                    selection = _remove_current_selection(
                        storage, result_key, node_name=name
                    )
                    if selection is not None:
                        invalidated.add(selection)
                invalidated.update(
                    _clear_currents_for_node(
                        self.storage_path,
                        name,
                        kind="dataframe_tool",
                    )
                )
            if isinstance(node.tool, ProcessingTool):
                if result_key is not None:
                    selection = _remove_current_selection(
                        storage, result_key, node_name=name
                    )
                    if selection is not None:
                        invalidated.add(selection)
                invalidated.update(
                    _clear_currents_for_node(
                        self.storage_path,
                        name,
                        kind="processing_tool",
                    )
                )
        return invalidated

    def _dataframe_tool_signature_params(self, node: Node) -> dict[str, Any]:
        from bioimageflow.validation import is_path_type

        input_annotations = node.tool.Inputs._get_all_annotations()
        args_dict = dict(node._constant_bindings)
        for field_name in input_annotations:
            if field_name not in args_dict and hasattr(node.tool.Inputs, field_name):
                args_dict[field_name] = getattr(node.tool.Inputs, field_name)
            if field_name in args_dict and is_path_type(input_annotations[field_name]):
                args_dict[field_name] = _absolute_runtime_path(args_dict[field_name])
        return args_dict

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
        """Return a per-node cache-status plan.

        Instantiates a non-Wetlands :class:`DefaultEngine` and calls its
        :meth:`plan`. No tool code runs.
        """
        from bioimageflow.engine import DefaultEngine

        self._dev_mode = dev_mode
        self._discover_graph(list(self._nodes.values()))
        return DefaultEngine(use_wetlands=False).plan(self)
