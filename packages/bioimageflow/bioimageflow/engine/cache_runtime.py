"""Focused methods extracted from the execution engine."""

# Pyright checks the complete contract on DefaultEngine; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from .common import (
    Any,
    Node,
    ProcessingTool,
    Storage,
    _path_output_columns,
    _shared_array_output_columns,
    canonical_dataframe_digest,
    compute_env_hash,
    dataframe_lookup,
    dataframe_result_key,
    pd,
    processing_lookup,
    processing_result_key,
    source_processing_signature_material,
)


class _CacheRuntimeMixin:
    def _set_node_cache_hit(self, node: Node, cache_hit: bool) -> None:
        with self._cache_hit_lock:
            self._node_cache_hits[node] = cache_hit

    def _pop_node_cache_hit(self, node: Node) -> bool:
        with self._cache_hit_lock:
            return self._node_cache_hits.pop(node, False)

    def _write_run_node_view(
        self,
        workflow: Any,
        node: Node,
        sig_hash: str | None,
        *,
        cache_hit: bool,
    ) -> None:
        context = getattr(workflow, "_run_view_context", None)
        if context is None or sig_hash is None:
            return
        from bioimageflow.dataframe_tool import DataFrameTool

        if isinstance(node.tool, DataFrameTool):
            result_key = dataframe_result_key(node.name, sig_hash)
        elif isinstance(node.tool, ProcessingTool):
            result_key = processing_result_key(node.name, sig_hash)
        else:
            return
        storage = Storage(workflow.storage_path)
        pointer = storage.load_current(result_key)
        if pointer is None:
            return
        run_id = str(context["run_id"])
        node_key = node.name
        storage.write_run_node_result(
            run_id,
            node_key,
            result_key=result_key,
            record_id=pointer.record_id,
            cache_hit=cache_hit,
        )
        storage.update_latest_node(node_key, run_id)
        auto_export = getattr(workflow, "_auto_export_outputs", None)
        if auto_export is not None:
            auto_export(run_id, latest_node=node_key, runs=False)

    def _selected_record_id(self, workflow: Any, result_key: str) -> str | None:
        pointer = Storage(workflow.storage_path).load_current(result_key)
        if pointer is None:
            return None
        return pointer.record_id

    def _node_result_key(self, node: Node, sig_hash: str) -> str | None:
        from bioimageflow.dataframe_tool import DataFrameTool

        if isinstance(node.tool, DataFrameTool):
            return dataframe_result_key(node.name, sig_hash)
        if isinstance(node.tool, ProcessingTool):
            return processing_result_key(node.name, sig_hash)
        return None

    # ── Graph traversal ────────────────────────────────────────────────

    def _check_node_cache(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str | None],
        workflow: Any,
        *,
        hydrate_assets: bool = True,
    ) -> tuple[pd.DataFrame | None, str | None]:
        """Check whether a node's result is already cached.

        Returns ``(cached_df, sig_hash)`` if a cache hit is found, or
        ``(None, sig_hash)`` if computable but not cached.  Returns
        ``(None, None)`` for WorkflowNodes (boundaries do not own cache entries).
        """
        from bioimageflow.dataframe_tool import DataFrameTool
        from bioimageflow.workflow_node import WorkflowNode

        if isinstance(node, WorkflowNode):
            return None, None

        # ── Compute signature hash ──
        if isinstance(node.tool, DataFrameTool):
            _arguments, args_dict = self._resolve_constant_arguments(node)
            for index, argument in enumerate(node._args):
                if isinstance(argument, pd.DataFrame):
                    args_dict[f"workflow_dataframe_input_{index}"] = (
                        canonical_dataframe_digest(argument)
                    )
            upstream_identities = self._upstream_identity_map(
                workflow,
                self._dataframe_upstream_recipes(node),
                sig_hashes,
            )
            if upstream_identities is None:
                return None, None
            sig_hash = self._compute_sig_hash(
                node, "", args_dict, upstream_identities, workflow
            )
        elif isinstance(node.tool, ProcessingTool):
            if not node._column_bindings:
                env_hash = compute_env_hash(node.tool.environment.dependencies)
                sig_hash = self._compute_sig_hash(
                    node,
                    env_hash,
                    source_processing_signature_material(node),
                    {},
                    workflow,
                )
            else:
                input_annotations = node.tool.Inputs._get_all_annotations()
                upstream_nodes = {
                    cr.node.name: cr.node for cr in node._column_bindings.values()
                }
                sig_hash = self._compute_processing_sig_hash(
                    node,
                    input_annotations,
                    upstream_nodes,
                    sig_hashes,
                    workflow,
                )
                if sig_hash is None:
                    return None, None
        else:
            return None, None

        # ── Cache lookup ──
        if isinstance(node.tool, DataFrameTool):
            df = dataframe_lookup(workflow.storage_path, node.name, sig_hash)
            if df is None:
                return None, sig_hash
            df = self._coerce_numeric_columns(df)
            return self._normalize_path_output_columns(df, node.tool), sig_hash
        if isinstance(node.tool, ProcessingTool):
            df = processing_lookup(
                workflow.storage_path,
                node.name,
                sig_hash,
                _path_output_columns(node.tool),
                shared_array_columns=_shared_array_output_columns(node.tool),
                hydrate_assets=hydrate_assets,
            )
            if df is None:
                return None, sig_hash
            df = self._coerce_numeric_columns(df)
            return self._normalize_path_output_columns(df, node.tool), sig_hash
        return None, sig_hash

    # ── Node dispatch ──────────────────────────────────────────────────
