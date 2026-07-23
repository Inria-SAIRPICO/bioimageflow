"""Focused methods extracted from the execution engine."""

# Pyright checks the complete contract on DefaultEngine; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from .common import (
    Any,
    Node,
    ProcessingTool,
    Storage,
    WorkflowCancelledError,
    _declared_owned_artifact_paths,
    _declared_zero_row_scalar_outputs,
    _explicit_template_output_columns,
    _path_output_columns,
    _resolve_staged_output_path,
    _shared_array_output_columns,
    canonical_dataframe_digest,
    compute_env_hash,
    dataframe_lookup,
    dataframe_publish,
    dataframe_result_key,
    get_output_templates,
    is_path_type,
    pd,
    processing_lookup,
    processing_prepare_attempt,
    processing_publish,
    processing_result_key,
    source_processing_signature_material,
)


class _NodeExecutionMixin:
    def _execute_node(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str | None],
        workflow: Any,
    ) -> tuple[pd.DataFrame, str | None]:
        """Execute a single node, returning its DataFrame and logical digest."""
        from bioimageflow.dataframe_tool import DataFrameTool
        from bioimageflow.workflow_node import WorkflowNode

        try:
            if isinstance(node, WorkflowNode):
                return self._execute_workflow_node(node, results, sig_hashes, workflow)
            elif isinstance(node.tool, DataFrameTool):
                return self._execute_dataframe_tool(node, results, sig_hashes, workflow)
            elif isinstance(node.tool, ProcessingTool):
                if not node._column_bindings:
                    return self._execute_source_processing_tool(
                        node, results, sig_hashes, workflow
                    )
                else:
                    return self._execute_processing_tool_with_column_bindings(
                        node, results, sig_hashes, workflow
                    )
            else:
                raise RuntimeError(f"Unknown tool type: {type(node.tool)}")
        except WorkflowCancelledError:
            self._emit_progress(workflow, node.name, "cancelled")
            raise
        except Exception as exc:
            self._emit_progress(workflow, node.name, "failed")
            if "/" in node.name and node.name not in str(exc):
                exc.args = (f"Node '{node.name}' failed: {exc}", *exc.args[1:])
            raise

    # ── DataFrameTool execution ────────────────────────────────────────

    def _execute_dataframe_tool(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str | None],
        workflow: Any,
    ) -> tuple[pd.DataFrame, str | None]:
        """Execute a DataFrameTool node."""
        from bioimageflow.dataframe_tool import DataFrameTool

        assert isinstance(node.tool, DataFrameTool)

        dfs = [
            results[arg] if isinstance(arg, Node) else arg
            for arg in node._args
            if (isinstance(arg, Node) and arg in results)
            or isinstance(arg, pd.DataFrame)
        ]
        arguments, args_dict = self._resolve_constant_arguments(node)
        for index, arg in enumerate(node._args):
            if isinstance(arg, pd.DataFrame):
                args_dict[f"workflow_dataframe_input_{index}"] = (
                    canonical_dataframe_digest(arg)
                )

        upstream_identities = self._upstream_identity_map(
            workflow,
            self._dataframe_upstream_recipes(node),
            sig_hashes,
        )
        sig_hash = (
            self._compute_sig_hash(
                node,
                "",
                args_dict,
                upstream_identities,
                workflow,
            )
            if upstream_identities is not None
            else None
        )

        result_key = (
            dataframe_result_key(node.name, sig_hash)
            if sig_hash is not None
            else None
        )
        if sig_hash is not None:
            cached = dataframe_lookup(workflow.storage_path, node.name, sig_hash)
            if cached is not None:
                self._set_node_cache_hit(node, True)
                self._emit_progress(
                    workflow,
                    node.name,
                    "cached",
                    result_key=result_key,
                    record_id=self._selected_record_id(workflow, result_key),
                )
                df = self._coerce_numeric_columns(cached)
                return self._normalize_path_output_columns(df, node.tool), sig_hash

        self._emit_progress(workflow, node.name, "started", result_key=result_key)

        if len(dfs) > 1:
            dfs = self._align_dataframes_for_merge(dfs)
        merged = node.tool.merge_dataframes(dfs, arguments)
        merged = self._coerce_numeric_columns(merged)
        df = node.tool.transform(merged, arguments)
        df = self._coerce_numeric_columns(df)
        df = self._normalize_path_output_columns(df, node.tool)
        df.index = df.index.astype(str)

        if sig_hash is not None:
            df = self._coerce_numeric_columns(
                dataframe_publish(
                    workflow.storage_path,
                    node.name,
                    sig_hash,
                    df,
                    run_id=str(workflow._run_view_context["run_id"]),
                    column_kinds={
                        column: "external_path"
                        for column in _path_output_columns(node.tool)
                    },
                )
            )
        self._emit_progress(
            workflow,
            node.name,
            "completed",
            result_key=result_key,
            record_id=(
                self._selected_record_id(workflow, result_key)
                if result_key is not None
                else None
            ),
        )
        df = self._normalize_path_output_columns(df, node.tool)
        self._set_node_cache_hit(node, False)
        return df, sig_hash

    # ── ProcessingTool execution ───────────────────────────────────────

    def _execute_source_processing_tool(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str | None],
        workflow: Any,
    ) -> tuple[pd.DataFrame, str | None]:
        """Execute a ProcessingTool node that has no upstream column bindings (source node)."""
        assert isinstance(node.tool, ProcessingTool)

        input_annotations = node.tool.Inputs._get_all_annotations()
        assert node.tool.Outputs is not None  # ProcessingTool always has Outputs
        templates = get_output_templates(
            node.tool.Outputs,
            node.tool.Inputs,
            node.output_templates,
        )

        aligned_index: list[Any] = ["0"]

        env_hash = compute_env_hash(node.tool.environment.dependencies)
        sig_hash = self._compute_sig_hash(
            node,
            env_hash,
            source_processing_signature_material(node),
            {},
            workflow,
        )

        # --- Cache check ---
        path_output_columns = _path_output_columns(node.tool)
        shared_array_output_columns = _shared_array_output_columns(node.tool)
        result_key = processing_result_key(node.name, sig_hash)
        cached = processing_lookup(
            workflow.storage_path,
            node.name,
            sig_hash,
            path_output_columns,
            shared_array_columns=shared_array_output_columns,
        )
        if cached is not None:
            self._set_node_cache_hit(node, True)
            self._emit_progress(
                workflow,
                node.name,
                "cached",
                result_key=result_key,
                record_id=self._selected_record_id(workflow, result_key),
            )
            df = self._coerce_numeric_columns(cached)
            return self._normalize_path_output_columns(df, node.tool), sig_hash

        # --- Resolve arguments ---
        self._emit_progress(workflow, node.name, "started", result_key=result_key)
        invocation_id = Storage(workflow.storage_path).new_invocation_id()
        result_key, attempt_id, staging_dir, real_assets_dir = (
            processing_prepare_attempt(
                workflow.storage_path,
                node.name,
                sig_hash,
            )
        )

        row_args = self._resolve_defaults(node, input_annotations)
        path_input_fields = [n for n, a in input_annotations.items() if is_path_type(a)]
        context = self._build_template_context(
            node.name,
            "0",
            row_args,
            path_input_fields=path_input_fields,
            upstream_nodes={},
            results={},
            idx="0",
        )
        for out_field, template in templates.items():
            row_args[out_field] = _resolve_staged_output_path(
                real_assets_dir, template, context
            )
        arguments_dicts = [row_args]

        # --- Dispatch & build output ---
        row_contexts, batch_context = self._build_execution_contexts(
            staging_dir,
            real_assets_dir,
            aligned_index,
        )
        raw_results = self._dispatch_tool(
            node.tool,
            arguments_dicts,
            workflow,
            node.name,
            row_contexts,
            batch_context,
            invocation_id=invocation_id,
            cache_attempt_id=attempt_id,
        )
        df = self._build_output_dataframe(raw_results, aligned_index, node.tool)
        owned_path_columns = _explicit_template_output_columns(node)
        declared_path_columns = set(templates)
        df = processing_publish(
            workflow.storage_path,
            node.name,
            sig_hash,
            df,
            result_key=result_key,
            attempt_id=attempt_id,
            run_id=str(workflow._run_view_context["run_id"]),
            staging_dir=staging_dir,
            staging_assets_dir=real_assets_dir,
            path_columns=path_output_columns,
            owned_path_columns=owned_path_columns,
            shared_array_columns=shared_array_output_columns,
            declared_owned_artifact_paths=_declared_owned_artifact_paths(
                arguments_dicts,
                aligned_index,
                df,
                declared_path_columns,
            ),
            declared_scalar_outputs=_declared_zero_row_scalar_outputs(
                node.tool,
                raw_results,
                aligned_index,
            ),
        )
        df = self._coerce_numeric_columns(df)
        df = self._normalize_path_output_columns(df, node.tool)
        self._emit_progress(
            workflow,
            node.name,
            "completed",
            result_key=result_key,
            record_id=self._selected_record_id(workflow, result_key),
        )
        self._set_node_cache_hit(node, False)
        return df, sig_hash

    def _execute_processing_tool_with_column_bindings(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str | None],
        workflow: Any,
    ) -> tuple[pd.DataFrame, str | None]:
        """Execute a ProcessingTool node that has upstream column bindings."""
        assert isinstance(node.tool, ProcessingTool)

        input_annotations = node.tool.Inputs._get_all_annotations()
        assert node.tool.Outputs is not None  # ProcessingTool always has Outputs
        templates = get_output_templates(
            node.tool.Outputs,
            node.tool.Inputs,
            node.output_templates,
        )

        upstream_nodes = {
            cr.node.name: cr.node for cr in node._column_bindings.values()
        }
        aligned_index, _ = self._align_indices(node, upstream_nodes, results)
        self._validate_column_bindings(node, results)

        # --- Signature hash ---
        sig_hash = self._compute_processing_sig_hash(
            node,
            input_annotations,
            upstream_nodes,
            sig_hashes,
            workflow,
        )

        # --- Cache check ---
        path_output_columns = _path_output_columns(node.tool)
        shared_array_output_columns = _shared_array_output_columns(node.tool)
        result_key = (
            processing_result_key(node.name, sig_hash)
            if sig_hash is not None
            else None
        )
        if sig_hash is not None:
            cached = processing_lookup(
                workflow.storage_path,
                node.name,
                sig_hash,
                path_output_columns,
                shared_array_columns=shared_array_output_columns,
            )
            if cached is not None:
                self._set_node_cache_hit(node, True)
                self._emit_progress(
                    workflow,
                    node.name,
                    "cached",
                    result_key=result_key,
                    record_id=self._selected_record_id(workflow, result_key),
                )
                df = self._coerce_numeric_columns(cached)
                return self._normalize_path_output_columns(df, node.tool), sig_hash

        # --- Resolve arguments ---
        self._emit_progress(workflow, node.name, "started", result_key=result_key)
        storage = Storage(workflow.storage_path)
        run_id = str(workflow._run_view_context["run_id"])
        invocation_id = storage.new_invocation_id()
        attempt_id: str | None = None
        transient = sig_hash is None
        if transient:
            invocation_id, staging_dir, real_assets_dir = (
                storage.create_transient_invocation(
                    run_id,
                    node.name,
                    invocation_id=invocation_id,
                    engine=self.backend_name,
                )
            )
        else:
            assert sig_hash is not None
            result_key, attempt_id, staging_dir, real_assets_dir = (
                processing_prepare_attempt(
                    workflow.storage_path,
                    node.name,
                    sig_hash,
                )
            )

        try:
            path_input_fields = [
                n for n, a in input_annotations.items() if is_path_type(a)
            ]
            execution_index = aligned_index
            has_batch = (
                type(node.tool).process_batch is not ProcessingTool.process_batch
            )
            if (
                not aligned_index
                and has_batch
                and getattr(node.tool, "run_empty_batch", False)
            ):
                execution_index, arguments_dicts = (
                    self._resolve_empty_batch_arguments(
                        node,
                        results,
                        input_annotations,
                        templates,
                        path_input_fields,
                        real_assets_dir,
                    )
                )
            else:
                arguments_dicts = self._resolve_all_row_arguments(
                    node,
                    aligned_index,
                    results,
                    upstream_nodes,
                    input_annotations,
                    templates,
                    path_input_fields,
                    real_assets_dir,
                )

            # --- Dispatch & build output ---
            row_contexts, batch_context = self._build_execution_contexts(
                staging_dir,
                real_assets_dir,
                execution_index,
            )
            raw_results = self._dispatch_tool(
                node.tool,
                arguments_dicts,
                workflow,
                node.name,
                row_contexts,
                batch_context,
                invocation_id=invocation_id,
                cache_attempt_id=attempt_id,
            )
            df = self._build_output_dataframe(
                raw_results,
                execution_index,
                node.tool,
            )
            if not transient:
                assert sig_hash is not None
                assert result_key is not None
                assert attempt_id is not None
                owned_path_columns = _explicit_template_output_columns(node)
                declared_path_columns = set(templates)
                df = processing_publish(
                    workflow.storage_path,
                    node.name,
                    sig_hash,
                    df,
                    result_key=result_key,
                    attempt_id=attempt_id,
                    run_id=run_id,
                    staging_dir=staging_dir,
                    staging_assets_dir=real_assets_dir,
                    path_columns=path_output_columns,
                    owned_path_columns=owned_path_columns,
                    shared_array_columns=shared_array_output_columns,
                    declared_owned_artifact_paths=_declared_owned_artifact_paths(
                        arguments_dicts,
                        execution_index,
                        df,
                        declared_path_columns,
                    ),
                    declared_scalar_outputs=_declared_zero_row_scalar_outputs(
                        node.tool,
                        raw_results,
                        execution_index,
                    ),
                )
            df = self._coerce_numeric_columns(df)
            df = self._normalize_path_output_columns(df, node.tool)
        except BaseException as exc:
            if transient:
                storage.finish_transient_invocation(
                    run_id,
                    node.name,
                    invocation_id,
                    status=(
                        "cancelled"
                        if isinstance(exc, WorkflowCancelledError)
                        else "failed"
                    ),
                    error=(
                        None
                        if isinstance(exc, WorkflowCancelledError)
                        else exc
                    ),
                )
            raise

        if transient:
            storage.finish_transient_invocation(
                run_id,
                node.name,
                invocation_id,
                status="succeeded",
            )

        self._emit_progress(
            workflow,
            node.name,
            "completed",
            result_key=result_key,
            record_id=(
                self._selected_record_id(workflow, result_key)
                if result_key is not None
                else None
            ),
        )
        self._set_node_cache_hit(node, False)
        return df, sig_hash

    # ── Argument resolution ────────────────────────────────────────────
