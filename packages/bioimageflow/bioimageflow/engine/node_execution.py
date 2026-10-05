"""Focused methods extracted from the execution engine."""

# Pyright checks the complete contract on DefaultEngine; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from dataclasses import dataclass, replace
from bioimageflow_core import Arguments, ConsumedRow
from bioimageflow.row_relation import ResultRelation, RowAssociation

from bioimageflow.cache.selection import SelectedResult

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

@dataclass(frozen=True)
class _ProviderExecutionResult:
    dataframe: pd.DataFrame
    signature_hash: str | None
    transient_invocation_id: str | None = None
    selection: SelectedResult | None = None

def _reject_reserved_source_indexes(
    dataframe: pd.DataFrame,
    *,
    source: str,
) -> None:
    reserved = [str(index) for index in dataframe.index if "::" in str(index)]
    if reserved:
        raise ValueError(
            f"{source} indexes must not contain the reserved '::' separator: "
            f"{reserved}."
        )


class _NodeExecutionMixin:
    def _execute_node(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str | None],
        workflow: Any,
    ) -> Any:
        from bioimageflow.dataframe_tool import DataFrameTool
        from bioimageflow.result_groups import map_shared_values as publish_frame
        import uuid

        self._adopt_node_inputs(node)
        owner = workflow.shared_memory_context
        scope = None
        try:
            if isinstance(node.tool, DataFrameTool):
                scope = owner.task_scope("dataframe_" + uuid.uuid4().hex)
                with scope.activate():
                    dataframe, signature = self._execute_node_bound(node, results, sig_hashes, workflow)
                dataframe = publish_frame(dataframe, scope.publish_value)
            else:
                with owner.activate():
                    dataframe, signature = self._execute_node_bound(node, results, sig_hashes, workflow)
            dataframe = self._bind_provider_groups(node, dataframe, workflow)
            selection = self._selected_result(node)
            if selection is not None:
                self._pin_selected_result(node, replace(selection, dataframe=dataframe))
            if scope is not None:
                scope.discard_unreturned()
            return dataframe, signature
        except BaseException:
            if scope is not None:
                scope.close()
            raise

    def _adopt_node_inputs(self, node: Node) -> None:
        """Capture producer bytes once before the execution's first key lookup."""
        from .shared_arrays import publish_inputs
        from bioimageflow.result_groups import bind_result_group
        import uuid

        admitted, _inputs_group = bind_result_group(
            publish_inputs([node._constant_bindings, node._args]),
            node_name=node.name, group_id="inputs_" + uuid.uuid4().hex,
        )
        node._constant_bindings, captured_args = admitted
        node._args = list(captured_args)

    def _bind_provider_groups(self, node: Node, dataframe: pd.DataFrame, workflow: Any, *, relation: ResultRelation | None = None, context: Any = None) -> pd.DataFrame:
        from bioimageflow.result_groups import bind_result_group
        from .shared_arrays import references
        import uuid

        if not any(tuple(references(cell)) for column in dataframe.columns for cell in dataframe[column].array):
            return dataframe
        relation = relation if relation is not None else self._result_relation(node)
        groups = relation.row_associations if relation is not None else (RowAssociation((), tuple(str(index) for index in dataframe.index)),)
        result = pd.DataFrame(dataframe, copy=False)
        positions = {str(index): position for position, index in enumerate(dataframe.index)}
        context = context if context is not None else getattr(workflow, "_active_run_context", None)
        for association in groups:
            selected_positions = [positions[index] for index in association.output_indices]
            section = pd.DataFrame(dataframe, copy=False).take(selected_positions)
            pinned, handle = bind_result_group(section, node_name=node.name,
                group_id=uuid.uuid4().hex, consumed_rows=association.consumed_rows)
            if handle is None:
                continue
            if context is not None:
                context._register_result_group(handle)
            for column_position, column in enumerate(dataframe.columns):
                if any(tuple(references(cell)) for cell in section[column].array):
                    result[column] = pd.Series(list(result[column].array), index=dataframe.index, dtype=object)
                    for local_position, output_position in enumerate(selected_positions):
                        result.iat[output_position, column_position] = pinned.iat[local_position, column_position]
        return result

    def _execute_node_bound(
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
            self._raise_if_cancelled(workflow)
            if not isinstance(node, WorkflowNode):
                self._capture_executable(node)
            if isinstance(node, WorkflowNode):
                dataframe, signature_hash = self._execute_workflow_node(
                    node, results, sig_hashes, workflow
                )
            elif isinstance(node.tool, DataFrameTool):
                provider_result = self._execute_dataframe_tool(
                    node, results, sig_hashes, workflow
                )
            elif isinstance(node.tool, ProcessingTool):
                if not node._column_bindings:
                    provider_result = self._execute_source_processing_tool(
                        node, results, sig_hashes, workflow
                    )
                else:
                    provider_result = (
                        self._execute_processing_tool_with_column_bindings(
                            node, results, sig_hashes, workflow
                        )
                    )
            else:
                raise RuntimeError(f"Unknown tool type: {type(node.tool)}")
            self._raise_if_cancelled(workflow)
            if isinstance(node, WorkflowNode):
                return dataframe, signature_hash
            self._record_provider_execution_outcome(
                workflow,
                node,
                provider_result,
            )
            return provider_result.dataframe, provider_result.signature_hash
        except WorkflowCancelledError:
            self._emit_progress(workflow, node.name, "cancelled")
            raise
        except Exception as exc:
            from bioimageflow.integration import NodeFailureDiagnostic

            self._emit_progress(
                workflow,
                node.name,
                "failed",
                diagnostic=NodeFailureDiagnostic.from_exception(
                    node.name,
                    exc,
                ),
            )
            if "/" in node.name and node.name not in str(exc):
                exc.args = (f"Node '{node.name}' failed: {exc}", *exc.args[1:])
            raise

    def _record_provider_execution_outcome(
        self,
        workflow: Any,
        node: Node,
        outcome: _ProviderExecutionResult,
    ) -> None:
        context = getattr(workflow, "_active_run_context", None)
        if context is None or context.terminal_status is not None:
            return

        result_key: str | None = None
        record_id: str | None = None
        if outcome.signature_hash is not None:
            result_key = self._node_result_key(node, outcome.signature_hash)
            if result_key is None:
                raise RuntimeError(
                    f"Provider {node.name!r} has no canonical result key."
                )
            selection = outcome.selection
            if selection is None or selection.result_key != result_key:
                raise RuntimeError("Provider outcome has no matching pinned record.")
            record_id = selection.record_id
            if record_id is None:
                raise RuntimeError(
                    f"Provider {node.name!r} has no selected immutable record."
                )

        context._record_provider_outcome(
            node_key=node.name,
            result_key=result_key,
            record_id=record_id,
            transient_invocation_id=outcome.transient_invocation_id,
            path_columns=_path_output_columns(node.tool),
            owned_path_columns=_explicit_template_output_columns(node),
            shared_array_columns=_shared_array_output_columns(node.tool),
        )

    # ── DataFrameTool execution ────────────────────────────────────────

    def _execute_dataframe_tool(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str | None],
        workflow: Any,
    ) -> _ProviderExecutionResult:
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
                _reject_reserved_source_indexes(
                    arg,
                    source="Root DataFrame",
                )
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

        result_key = dataframe_result_key(node.name, sig_hash) if sig_hash is not None else None
        if sig_hash is not None:
            cached = dataframe_lookup(workflow.storage_path, node.name, sig_hash)
            if cached is not None:
                self._pin_selected_result(node, cached)
                self._set_node_cache_hit(node, True)
                self._emit_progress(
                    workflow,
                    node.name,
                    "cached",
                    result_key=result_key,
                    record_id=self._pinned_record_id(node),
                )
                df = cached.dataframe
                return _ProviderExecutionResult(
                    self._normalize_path_output_columns(df, node.tool),
                    sig_hash,
                    selection=cached,
                )

        self._emit_progress(workflow, node.name, "started", result_key=result_key)

        if len(dfs) > 1:
            dfs = self._align_dataframes_for_merge(dfs)
        capture = self._capture_executable(node)
        with capture.execution_context():
            merged = capture.callbacks["merge_dataframes"](dfs, arguments)
            df = capture.callbacks["transform"](merged, arguments)
        df = self._normalize_path_output_columns(df, node.tool)
        df.index = df.index.astype(str)
        if not dfs:
            _reject_reserved_source_indexes(
                df,
                source=f"Source DataFrameTool {node.name!r}",
            )

        consumed = tuple(ConsumedRow(position, str(index)) for position, index in enumerate(merged.index))
        relation = ResultRelation(
            "dataframe", f"{'merge' if dfs else 'source'}::{node.name}::{result_key or node.name}",
            "merge" if dfs else "source", (RowAssociation(consumed, tuple(str(index) for index in df.index)),),
        )
        self._node_result_relations[node] = relation
        self._raise_if_cancelled(workflow)
        if sig_hash is not None:
            selection = dataframe_publish(
                workflow.storage_path,
                node.name,
                sig_hash,
                df,
                run_id=str(workflow._run_view_context["run_id"]),
                engine=self._effective_engine_name(workflow),
                tool_identity=(
                    f"{type(node.tool).__module__}:{type(node.tool).__qualname__}"
                ),
                row_relation=relation.to_dict(),
                column_kinds={
                    column: "external_path"
                    for column in _path_output_columns(node.tool)
                },
            )
            self._pin_selected_result(node, selection)
            df = selection.dataframe
        self._emit_progress(
            workflow,
            node.name,
            "completed",
            result_key=result_key,
            record_id=(
                self._pinned_record_id(node) if result_key is not None else None
            ),
        )
        df = self._normalize_path_output_columns(df, node.tool)
        self._set_node_cache_hit(node, False)
        return _ProviderExecutionResult(
            df, sig_hash, selection=self._selected_result(node)
        )

    # ── ProcessingTool execution ───────────────────────────────────────

    def _execute_source_processing_tool(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str | None],
        workflow: Any,
    ) -> _ProviderExecutionResult:
        """Execute a ProcessingTool node that has no upstream column bindings (source node)."""
        assert isinstance(node.tool, ProcessingTool)

        input_annotations = node.tool.Inputs._get_all_annotations()
        assert node.tool.Outputs is not None  # ProcessingTool always has Outputs
        templates = get_output_templates(
            node.tool.Outputs,
            node.tool.Inputs,
            node.output_templates,
        )

        collective = node.tool.row_consumption.value == "collective"
        aligned_index: list[Any] = [] if collective else ["0"]

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
            self._pin_selected_result(node, cached)
            self._set_node_cache_hit(node, True)
            self._emit_progress(
                workflow,
                node.name,
                "cached",
                result_key=result_key,
                record_id=self._pinned_record_id(node),
            )
            df = cached.dataframe
            return _ProviderExecutionResult(
                self._normalize_path_output_columns(df, node.tool),
                sig_hash,
                selection=cached,
            )

        # --- Resolve arguments ---
        self._emit_progress(workflow, node.name, "started", result_key=result_key)
        storage = Storage(workflow.storage_path)
        run_id = str(workflow._run_view_context["run_id"])
        invocation_id = storage.new_invocation_id()
        result_key, attempt_id, staging_dir, real_assets_dir = (
            processing_prepare_attempt(
                workflow.storage_path,
                node.name,
                sig_hash,
                run_id=run_id,
                invocation_id=invocation_id,
                engine=self._effective_engine_name(workflow),
                tool_identity=(
                    f"{type(node.tool).__module__}:{type(node.tool).__qualname__}"
                ),
            )
        )

        try:
            row_args = self._resolve_defaults(node, input_annotations)
            path_input_fields = [
                name
                for name, annotation in input_annotations.items()
                if is_path_type(annotation)
            ]
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
            arguments_dicts = [] if collective else [row_args]
            batch_values, reference_rows = row_args, ()

            # --- Dispatch & build output ---
            row_contexts, batch_context = self._build_execution_contexts(
                staging_dir,
                real_assets_dir,
                aligned_index,
            )
            batch_context = replace(batch_context, batch_arguments=Arguments(**batch_values), reference_rows=reference_rows)
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
            assembled = self._build_output_dataframe(
                raw_results, aligned_index, node.tool, row_consumption=node.tool.row_consumption.value,
                node_name=node.name, result_key=result_key,
            )
            df = assembled.dataframe
            self._node_result_relations[node] = assembled.relation
            self._raise_if_cancelled(workflow)
            owned_path_columns = _explicit_template_output_columns(node)
            declared_path_columns = set(templates)
            selection = processing_publish(
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
                    aligned_index,
                    df,
                    declared_path_columns,
                ),
                row_relation=assembled.relation.to_dict(),
                declared_scalar_outputs=_declared_zero_row_scalar_outputs(
                    node.tool,
                    raw_results,
                    aligned_index,
                ),
            )
            self._pin_selected_result(node, selection)
            df = selection.dataframe
        except BaseException as exc:
            storage.finish_cache_attempt(
                result_key,
                attempt_id,
                status=(
                    "cancelled" if isinstance(exc, WorkflowCancelledError) else "failed"
                ),
                error_type=(
                    None
                    if isinstance(exc, WorkflowCancelledError)
                    else type(exc).__name__
                ),
            )
            raise
        storage.finish_cache_attempt(
            result_key,
            attempt_id,
            status="succeeded",
        )
        df = self._normalize_path_output_columns(df, node.tool)
        self._emit_progress(
            workflow,
            node.name,
            "completed",
            result_key=result_key,
            record_id=self._pinned_record_id(node),
        )
        self._set_node_cache_hit(node, False)
        return _ProviderExecutionResult(
            df, sig_hash, selection=self._selected_result(node)
        )

    def _execute_processing_tool_with_column_bindings(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str | None],
        workflow: Any,
    ) -> _ProviderExecutionResult:
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
        collective = node.tool.row_consumption.value == "collective"
        reference_inputs = set(getattr(node.tool, "collective_reference_inputs", ())) if collective else set()
        driving_upstream_nodes = {
            reference.node.name: reference.node for field, reference in node._column_bindings.items()
            if field not in reference_inputs
        }
        aligned_index, _ = self._align_indices(node, driving_upstream_nodes, results)
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
            processing_result_key(node.name, sig_hash) if sig_hash is not None else None
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
                self._pin_selected_result(node, cached)
                self._set_node_cache_hit(node, True)
                self._emit_progress(
                    workflow,
                    node.name,
                    "cached",
                    result_key=result_key,
                    record_id=self._pinned_record_id(node),
                )
                df = cached.dataframe
                return _ProviderExecutionResult(
                    self._normalize_path_output_columns(df, node.tool),
                    sig_hash,
                    selection=cached,
                )

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
                    engine=self._effective_engine_name(workflow),
                )
            )
        else:
            assert sig_hash is not None
            result_key, attempt_id, staging_dir, real_assets_dir = (
                processing_prepare_attempt(
                    workflow.storage_path,
                    node.name,
                    sig_hash,
                    run_id=run_id,
                    invocation_id=invocation_id,
                    engine=self._effective_engine_name(workflow),
                    tool_identity=(
                        f"{type(node.tool).__module__}:{type(node.tool).__qualname__}"
                    ),
                )
            )

        try:
            path_input_fields = [
                n for n, a in input_annotations.items() if is_path_type(a)
            ]
            execution_index = aligned_index
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
            batch_values, reference_rows = self._resolve_collective_context(
                node, results, input_annotations, templates, path_input_fields, real_assets_dir,
            ) if collective else ({}, ())

            # --- Dispatch & build output ---
            row_contexts, batch_context = self._build_execution_contexts(
                staging_dir,
                real_assets_dir,
                execution_index,
            )
            batch_context = replace(batch_context, batch_arguments=Arguments(**batch_values), reference_rows=reference_rows)
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
            input_relations = [self._result_relation(provider) for provider in upstream_nodes.values()]
            input_relation = next((relation for relation in input_relations if relation is not None), None)
            assembled = self._build_output_dataframe(
                raw_results, execution_index, node.tool, row_consumption=node.tool.row_consumption.value,
                node_name=node.name, result_key=result_key,
                input_domain=None if input_relation is None else input_relation.output_domain,
                input_domain_kind=None if input_relation is None else input_relation.domain_kind,
            )
            df = assembled.dataframe
            self._node_result_relations[node] = assembled.relation
            self._raise_if_cancelled(workflow)
            if not transient:
                assert sig_hash is not None
                assert result_key is not None
                assert attempt_id is not None
                owned_path_columns = _explicit_template_output_columns(node)
                declared_path_columns = set(templates)
                selection = processing_publish(
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
                    row_relation=assembled.relation.to_dict(),
                declared_scalar_outputs=_declared_zero_row_scalar_outputs(
                        node.tool,
                        raw_results,
                        execution_index,
                    ),
                )
                self._pin_selected_result(node, selection)
                df = selection.dataframe
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
                    error=(None if isinstance(exc, WorkflowCancelledError) else exc),
                )
            else:
                assert result_key is not None
                assert attempt_id is not None
                storage.finish_cache_attempt(
                    result_key,
                    attempt_id,
                    status=(
                        "cancelled"
                        if isinstance(exc, WorkflowCancelledError)
                        else "failed"
                    ),
                    error_type=(
                        None
                        if isinstance(exc, WorkflowCancelledError)
                        else type(exc).__name__
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
        else:
            assert result_key is not None
            assert attempt_id is not None
            storage.finish_cache_attempt(
                result_key,
                attempt_id,
                status="succeeded",
            )

        self._emit_progress(
            workflow,
            node.name,
            "completed",
            result_key=result_key,
            record_id=(
                self._pinned_record_id(node) if result_key is not None else None
            ),
        )
        self._set_node_cache_hit(node, False)
        return _ProviderExecutionResult(
            df,
            sig_hash,
            transient_invocation_id=invocation_id if transient else None,
            selection=None if transient else self._selected_result(node),
        )
