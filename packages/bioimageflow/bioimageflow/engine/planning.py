"""Focused methods extracted from the execution engine."""

# Pyright checks the complete contract on DefaultEngine; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import (
    Any,
    CycleError,
    CycleInWorkflowError,
    Node,
    NodePlan,
    NodePlanStatus,
    ProcessingTool,
    Storage,
    TopologicalSorter,
    _dataframe_has_other_current,
    _processing_has_other_current,
    dataframe_result_key,
    hashlib,
    json,
    processing_result_key,
    scoped_node_names,
)

if TYPE_CHECKING:
    from bioimageflow.workflow_node import WorkflowNode


class _PlanningMixin:
    def plan(self, workflow: Any) -> dict[str, NodePlan]:
        """Return the cache status and diagnostic plan state of every node.

        Walks the graph in topological order, computes diagnostic logical
        signatures with the same helpers as :meth:`execute`, and reports
        result-key/current-record state when enough upstream cache selections
        are known. No tool code runs, and no Wetlands environment is launched.

        Nested workflow tools appear under scoped names
        (``"workflow_node/internal_name"``), matching
        :meth:`execute_steps`.

        Disabled nodes and nodes downstream of disabled nodes are
        returned with ``skipped=True`` and no final result key.

        Raises
        ------
        CycleInWorkflowError
            If the graph contains a cycle. Use :meth:`Workflow.validate`
            for non-fatal cycle reporting.
        """
        reachable, completion_dependencies, scoped_names = (
            self._compile_execution_graph(list(workflow._nodes.values()))
        )
        dep_graph = self._build_dep_graph_from_set(
            reachable,
            completion_dependencies,
        )
        try:
            order = list(TopologicalSorter(dep_graph).static_order())
        except CycleError as exc:
            cycle_nodes = exc.args[1] if len(exc.args) > 1 else []
            names = [getattr(n, "name", str(n)) for n in cycle_nodes]
            raise CycleInWorkflowError(names) from exc
        _executable, skipped = self._filter_executable(
            order,
            completion_dependencies,
        )

        plan: dict[str, NodePlan] = {}
        results: dict[Node, Any] = {}
        sig_hashes: dict[Node, str] = {}

        with scoped_node_names(scoped_names):
            for node in order:
                if node in skipped:
                    plan[node.name] = NodePlan(
                        node.name,
                        "",
                        NodePlanStatus.SKIPPED,
                        tuple(self._plan_upstream_names(node)),
                    )
                    continue
                self._plan_node(node, results, sig_hashes, workflow, plan)

        # Include any nodes not reachable from terminals (shouldn't happen
        # in practice, but plan is expected to cover every registered node).
        for name, node in workflow._nodes.items():
            plan.setdefault(
                name,
                NodePlan(
                    name,
                    "",
                    NodePlanStatus.SKIPPED,
                    (),
                ),
            )
        return plan

    def _plan_node(
        self,
        node: Node,
        results: dict[Node, Any],
        sig_hashes: dict[Node, str],
        workflow: Any,
        plan: dict[str, NodePlan],
    ) -> None:
        """Compute a NodePlan for a single node, recursing into workflows."""
        from bioimageflow.dataframe_tool import DataFrameTool
        from bioimageflow.workflow_node import WorkflowNode

        if isinstance(node, WorkflowNode):
            self._plan_compiled_workflow_node(node, sig_hashes, plan)
            return

        cached_df, sig_hash = self._check_node_cache(
            node,
            results,
            sig_hashes,
            workflow,
            hydrate_assets=False,
        )
        if sig_hash is None:
            # Workflow boundaries are handled above; executable tools are cacheable.
            plan[node.name] = NodePlan(
                node.name,
                "",
                NodePlanStatus.UNEXECUTED,
                tuple(self._plan_upstream_names(node)),
            )
            return
        sig_hashes[node] = sig_hash
        upstream = tuple(self._plan_upstream_names(node))
        pending_upstreams = tuple(
            name
            for name in upstream
            if (entry := plan.get(name)) is not None
            and entry.selected_record_id is None
            and entry.status not in {NodePlanStatus.CACHED, NodePlanStatus.SKIPPED}
        )
        final_result_key = (
            self._plan_final_result_key(node, sig_hash)
            if not pending_upstreams
            else None
        )
        selected_record_id = self._plan_selected_record_id(workflow, final_result_key)
        if pending_upstreams:
            status = NodePlanStatus.PENDING_UPSTREAM
        elif cached_df is not None:
            status = NodePlanStatus.CACHED
        else:
            if isinstance(node.tool, DataFrameTool):
                has_prior_current = _dataframe_has_other_current(
                    workflow.storage_path,
                    node.name,
                    sig_hash,
                )
            elif isinstance(node.tool, ProcessingTool):
                has_prior_current = _processing_has_other_current(
                    workflow.storage_path,
                    node.name,
                    sig_hash,
                )
            else:
                has_prior_current = False
            if has_prior_current:
                status = NodePlanStatus.PRIOR_SELECTION_MISS
            else:
                status = NodePlanStatus.UNEXECUTED
        plan[node.name] = NodePlan(
            node.name,
            sig_hash,
            status,
            upstream,
            final_result_key=final_result_key,
            selected_record_id=selected_record_id,
            pending_upstreams=pending_upstreams,
        )

    def _plan_final_result_key(self, node: Node, sig_hash: str) -> str | None:
        from bioimageflow.dataframe_tool import DataFrameTool

        if isinstance(node.tool, DataFrameTool):
            return dataframe_result_key(node.name, sig_hash)
        if isinstance(node.tool, ProcessingTool):
            return processing_result_key(node.name, sig_hash)
        return None

    def _plan_selected_record_id(
        self, workflow: Any, final_result_key: str | None
    ) -> str | None:
        if final_result_key is None:
            return None
        pointer = Storage(workflow.storage_path).load_current(final_result_key)
        return pointer.record_id if pointer is not None else None

    def _plan_compiled_workflow_node(
        self,
        node: "WorkflowNode",
        sig_hashes: dict[Node, str],
        plan: dict[str, NodePlan],
    ) -> None:
        """Reduce already-planned compiled internals to one boundary entry."""
        terminal_hashes = {
            field: sig_hashes[col_ref.node]
            for field, col_ref in node._published_outputs.items()
            if col_ref.node in sig_hashes
        }
        combined = hashlib.sha256(
            json.dumps(terminal_hashes, sort_keys=True).encode()
        ).hexdigest()
        sig_hashes[node] = combined
        internal_statuses = [
            plan[internal.name].status
            for internal in node.internal_nodes
            if internal.name in plan
        ]
        if any(
            status is NodePlanStatus.PENDING_UPSTREAM for status in internal_statuses
        ):
            status = NodePlanStatus.PENDING_UPSTREAM
        elif internal_statuses and all(
            status in {NodePlanStatus.CACHED, NodePlanStatus.SKIPPED}
            for status in internal_statuses
        ):
            status = NodePlanStatus.CACHED
        else:
            status = NodePlanStatus.UNEXECUTED
        plan[node.name] = NodePlan(
            node.name,
            combined,
            status,
            tuple(
                dict.fromkeys(
                    [
                        *self._plan_upstream_names(node),
                        *(
                            reference.node.name
                            for reference in node._published_outputs.values()
                        ),
                    ]
                )
            ),
            pending_upstreams=tuple(
                dict.fromkeys(
                    pending
                    for internal in node.internal_nodes
                    if (entry := plan.get(internal.name)) is not None
                    for pending in entry.pending_upstreams
                )
            ),
        )

    def _plan_upstream_names(self, node: Node) -> list[str]:
        names: list[str] = []
        for up in node._upstream_nodes:
            names.append(up.name)
        for arg in node._args:
            if isinstance(arg, Node):
                names.append(arg.name)
        return list(dict.fromkeys(names))
