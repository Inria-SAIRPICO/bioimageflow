"""Focused methods extracted from the execution engine."""

# Pyright checks the complete contract on DefaultEngine; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from .common import (
    Any,
    Arguments,
    ExecutionContext,
    Node,
    Path,
    ProcessingTool,
    Storage,
    _absolute_runtime_path,
    _batch_work_dir,
    _pending_assets_dir,
    _resolve_staged_output_path,
    _rows_work_dir,
    _safe_work_dir_name,
    _to_python,
    _work_dir,
    cast,
    compute_env_hash,
    compute_signature_hash,
    get_output_templates,
    get_source_hash,
    get_tool_version,
    is_path_type,
    pd,
    resolve_template,
    time,
)


class _ArgumentsMixin:
    def _resolve_constant_arguments(
        self,
        node: Node,
    ) -> tuple[Arguments, dict[str, Any]]:
        """Resolve constants + defaults into an Arguments object and raw dict."""
        input_annotations = node.tool.Inputs._get_all_annotations()
        args_dict = dict(node._constant_bindings)
        for field_name in input_annotations:
            if field_name not in args_dict and hasattr(node.tool.Inputs, field_name):
                args_dict[field_name] = getattr(node.tool.Inputs, field_name)
        self._normalize_path_arguments(args_dict, input_annotations)
        return Arguments(**args_dict), args_dict

    def _resolve_defaults(
        self,
        node: Node,
        input_annotations: dict[str, Any],
    ) -> dict[str, Any]:
        """Build args dict from constants and defaults (no column bindings)."""
        row_args = dict(node._constant_bindings)
        for field_name in input_annotations:
            if field_name not in row_args and hasattr(node.tool.Inputs, field_name):
                row_args[field_name] = getattr(node.tool.Inputs, field_name)
        self._normalize_path_arguments(row_args, input_annotations)
        return row_args

    def _normalize_path_arguments(
        self,
        args: dict[str, Any],
        input_annotations: dict[str, Any],
    ) -> None:
        """Convert path-typed input argument values to absolute runtime paths."""
        for field_name, annotation in input_annotations.items():
            if field_name in args and is_path_type(annotation):
                args[field_name] = _absolute_runtime_path(args[field_name])

    def _normalize_path_output_columns(
        self,
        df: pd.DataFrame,
        tool: Any,
    ) -> pd.DataFrame:
        """Convert declared path output columns to absolute runtime paths."""
        outputs = getattr(tool, "Outputs", None)
        if outputs is None or not hasattr(outputs, "_get_all_annotations"):
            return df
        output_annotations = outputs._get_all_annotations()
        path_columns = [
            field_name
            for field_name, annotation in output_annotations.items()
            if field_name in df.columns and is_path_type(annotation)
        ]
        if not path_columns:
            return df
        normalized = df.copy()
        for field_name in path_columns:
            normalized[field_name] = normalized[field_name].map(_absolute_runtime_path)
        return normalized

    def _resolve_all_row_arguments(
        self,
        node: Node,
        aligned_index: list[Any],
        results: dict[Node, pd.DataFrame],
        upstream_nodes: dict[str, Node],
        input_annotations: dict[str, Any],
        templates: dict[str, str],
        path_input_fields: list[str],
        workflow: Any,
        assets_dir: Path | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve per-row arguments for all rows in the aligned index."""
        arguments_dicts: list[dict[str, Any]] = []
        timestamp = str(int(time.time()))

        # Pre-compute index sets for O(1) lookup
        index_sets: dict[str, set[str]] = {
            n.name: set(str(i) for i in results[n].index)
            for n in upstream_nodes.values()
            if n in results
        }

        for idx in aligned_index:
            row_args = self._resolve_single_row(
                node,
                idx,
                results,
                input_annotations,
                index_sets,
            )
            context = self._build_template_context(
                node.name,
                str(idx),
                row_args,
                path_input_fields,
                upstream_nodes,
                results,
                idx,
                timestamp,
            )
            template_assets_dir = assets_dir or _pending_assets_dir(
                workflow.storage_path,
                node.name,
            )
            for out_field, template in templates.items():
                if assets_dir is None:
                    row_args[out_field] = str(
                        template_assets_dir / resolve_template(template, context)
                    )
                else:
                    row_args[out_field] = _resolve_staged_output_path(
                        template_assets_dir, template, context
                    )

            arguments_dicts.append(row_args)
        return arguments_dicts

    def _resolve_empty_batch_arguments(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        input_annotations: dict[str, Any],
        templates: dict[str, str],
        path_input_fields: list[str],
        assets_dir: Path,
    ) -> tuple[list[Any], list[dict[str, Any]]]:
        """Resolve constants/defaults and output templates for an empty batch."""
        anchor_inputs = tuple(getattr(node.tool, "empty_batch_anchor_inputs", ()))
        anchor_bindings = [
            (field, node._column_bindings[field])
            for field in anchor_inputs
            if field in node._column_bindings
            and node._column_bindings[field].node in results
            and not results[node._column_bindings[field].node].empty
        ]
        if anchor_bindings:
            anchor_df = results[anchor_bindings[0][1].node]
            execution_index = sorted(anchor_df.index, key=str)
        else:
            execution_index = ["0"]

        arguments_dicts: list[dict[str, Any]] = []
        for idx in execution_index:
            row_args = self._resolve_defaults(node, input_annotations)
            for field, col_ref in anchor_bindings:
                up_df = results[col_ref.node]
                idx_set = set(str(i) for i in up_df.index)
                resolved_idx = (
                    idx
                    if str(idx) in idx_set
                    else self._find_parent_index(idx, idx_set)
                )
                if resolved_idx is None:
                    continue
                row_args[field] = _to_python(
                    up_df.at[resolved_idx, self._column_label(col_ref)]
                )
            self._normalize_path_arguments(row_args, input_annotations)
            context = self._build_template_context(
                node.name,
                str(idx),
                row_args,
                path_input_fields,
                {},
                results,
                idx,
            )
            for out_field, template in templates.items():
                row_args[out_field] = _resolve_staged_output_path(
                    assets_dir, template, context
                )
            arguments_dicts.append(row_args)
        return execution_index, arguments_dicts

    def _resolve_single_row(
        self,
        node: Node,
        idx: Any,
        results: dict[Node, pd.DataFrame],
        input_annotations: dict[str, Any],
        index_sets: dict[str, set[str]] | None = None,
    ) -> dict[str, Any]:
        """Resolve column bindings, constants, and defaults for one row.

        Precedence: column bindings > constants > class-level defaults.
        Construction (Node.__init__, from_dict) usually enforces that a
        field is bound at most one way, but ``session.set_constant`` can
        leave a stale entry in ``_constant_bindings`` for a field that is
        also column-bound. In that case the upstream value must win —
        otherwise a stray ``None`` constant silently clobbers the row's
        real input (see the Files → Atlas regression).
        """
        row_args: dict[str, Any] = {}

        for field, col_ref in node._column_bindings.items():
            up_df = results[col_ref.node]
            idx_set = (index_sets or {}).get(col_ref.node.name) or set(up_df.index)
            if idx in idx_set:
                row_args[field] = _to_python(up_df.at[idx, self._column_label(col_ref)])
            else:
                parent_idx = self._find_parent_index(idx, idx_set)
                if parent_idx is not None:
                    row_args[field] = _to_python(
                        up_df.at[parent_idx, self._column_label(col_ref)]
                    )
                else:
                    raise KeyError(
                        f"Column '{col_ref.column}' not found for index '{idx}' "
                        f"in node '{col_ref.node.name}'"
                    )

        for field, value in node._constant_bindings.items():
            row_args.setdefault(field, value)

        for field_name in input_annotations:
            if field_name not in row_args and hasattr(node.tool.Inputs, field_name):
                row_args[field_name] = getattr(node.tool.Inputs, field_name)

        self._normalize_path_arguments(row_args, input_annotations)
        return row_args

    def _build_template_context(
        self,
        node_name: str,
        row_index: str,
        row_args: dict[str, Any],
        path_input_fields: list[str],
        upstream_nodes: dict[str, Node],
        results: dict[Node, pd.DataFrame],
        idx: Any,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Build the template variable context for a single row."""
        context: dict[str, Any] = {
            "node_name": node_name,
            "row_index": row_index.replace("::", "_"),
            "timestamp": timestamp or str(int(time.time())),
        }
        for field_name, value in row_args.items():
            context[field_name] = value

        path_values = [
            row_args[pf]
            for pf in path_input_fields
            if pf in row_args and row_args[pf] is not None
        ]
        if len(path_values) == 1:
            context["_ext"] = Path(str(path_values[0])).suffix
        else:
            context["_ext"] = ""

        # Collect upstream column values for {column:<name>}
        columns: dict[str, Any] = {}
        for up_node in upstream_nodes.values():
            up_df = results.get(up_node)
            if up_df is None:
                continue
            idx_set = set(str(i) for i in up_df.index)
            resolved_idx = (
                idx if idx in idx_set else self._find_parent_index(idx, idx_set)
            )
            if resolved_idx is not None:
                for col in up_df.columns:
                    columns[col] = up_df.at[resolved_idx, col]
        context["_columns"] = columns
        return context

    # ── Validation ─────────────────────────────────────────────────────

    def _validate_column_bindings(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
    ) -> None:
        """Check that all referenced columns exist in upstream DataFrames."""
        for field, col_ref in node._column_bindings.items():
            up_df = results[col_ref.node]
            column_label = self._column_label(col_ref)
            if column_label not in up_df.columns:
                from bioimageflow.node import ColumnNotFoundError

                raise ColumnNotFoundError(
                    f"Column '{column_label}' not found in output of node "
                    f"'{col_ref.node.name}'. Available columns: "
                    f"{list(up_df.columns)}"
                )

    # ── Signature hashing ──────────────────────────────────────────────

    def _upstream_identity_map(
        self,
        workflow: Any,
        upstream_nodes: list[Node],
        sig_hashes: dict[Node, str],
    ) -> dict[str, str]:
        """Return cache identity material for upstream nodes."""
        identities: dict[str, str] = {}
        storage = Storage(workflow.storage_path)
        for upstream in upstream_nodes:
            sig_hash = sig_hashes[upstream]
            result_key = self._node_result_key(upstream, sig_hash)
            if result_key is None:
                identities[upstream.name] = f"signature:{sig_hash}"
                continue
            pointer = storage.load_current(result_key)
            if pointer is None:
                identities[upstream.name] = f"signature:{sig_hash}"
            else:
                identities[upstream.name] = f"record:{result_key}:{pointer.record_id}"
        return identities

    def _compute_sig_hash(
        self,
        node: Node,
        env_hash: str,
        resolved_params: Any,
        upstream_hashes: dict[str, str],
        workflow: Any,
    ) -> str:
        """Compute the logical digest for any node type."""
        tool_version = get_tool_version(node.tool)
        source_hash = get_source_hash(type(node.tool)) if workflow._dev_mode else None
        return compute_signature_hash(
            type(node.tool).__name__,
            tool_version,
            env_hash,
            resolved_params,
            upstream_hashes,
            source_hash=source_hash,
        )

    def _compute_processing_sig_hash(
        self,
        node: Node,
        input_annotations: dict[str, Any],
        upstream_nodes: dict[str, Node],
        sig_hashes: dict[Node, str],
        workflow: Any,
    ) -> str:
        """Compute the logical digest for a non-source ProcessingTool."""
        env_hash = compute_env_hash(
            cast(ProcessingTool, node.tool).environment.dependencies
        )
        assert node.tool.Outputs is not None
        missing = [n.name for n in upstream_nodes.values() if n not in sig_hashes]
        if missing:
            raise RuntimeError(
                f"Cannot compute logical digest for node: upstream nodes "
                f"{missing} have not been executed yet."
            )
        upstream_hash_map = self._upstream_identity_map(
            workflow,
            list(upstream_nodes.values()),
            sig_hashes,
        )
        signature_constants = dict(node._constant_bindings)
        self._normalize_path_arguments(signature_constants, input_annotations)
        signature_defaults = {
            f: getattr(node.tool.Inputs, f)
            for f in input_annotations
            if f not in node._column_bindings
            and f not in node._constant_bindings
            and hasattr(node.tool.Inputs, f)
        }
        self._normalize_path_arguments(signature_defaults, input_annotations)
        resolved_params: dict[str, Any] = {
            "bindings": {
                f: {"node": cr.node.name, "column": cr.column}
                for f, cr in node._column_bindings.items()
            },
            "constants": signature_constants,
            "defaults": signature_defaults,
            "output_templates": get_output_templates(
                node.tool.Outputs,
                node.tool.Inputs,
                node.output_templates,
            ),
        }
        return self._compute_sig_hash(
            node,
            env_hash,
            resolved_params,
            upstream_hash_map,
            workflow,
        )

    # ── Dispatch & output construction ─────────────────────────────────

    def _build_execution_contexts(
        self,
        run_dir: Path,
        assets_dir: Path,
        aligned_index: list[Any],
    ) -> tuple[list[ExecutionContext], ExecutionContext]:
        """Create per-row and batch ProcessingTool execution contexts."""
        work_dir = _work_dir(run_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        rows_dir = _rows_work_dir(run_dir)
        rows_dir.mkdir(parents=True, exist_ok=True)
        row_contexts: list[ExecutionContext] = []
        for position, row_index in enumerate(aligned_index):
            row_dir = rows_dir / _safe_work_dir_name(position, row_index)
            row_dir.mkdir(parents=True, exist_ok=True)
            row_contexts.append(
                ExecutionContext(
                    run_dir=run_dir,
                    assets_dir=assets_dir,
                    work_dir=work_dir,
                    rows_dir=rows_dir,
                    row_dir=row_dir,
                    batch_dir=None,
                    row_index=str(row_index),
                )
            )

        batch_dir = _batch_work_dir(run_dir)
        batch_dir.mkdir(parents=True, exist_ok=True)
        batch_context = ExecutionContext(
            run_dir=run_dir,
            assets_dir=assets_dir,
            work_dir=work_dir,
            rows_dir=rows_dir,
            row_dir=None,
            batch_dir=batch_dir,
            row_index=None,
        )
        return row_contexts, batch_context

    def _fixup_output_paths(
        self,
        arguments_dicts: list[dict[str, Any]],
        templates: dict[str, str],
        real_assets_dir: Path,
    ) -> None:
        """Replace pending-dir paths with real hash dir paths."""
        for row_args in arguments_dicts:
            for out_field in templates:
                if out_field in row_args:
                    filename = Path(row_args[out_field]).name
                    row_args[out_field] = str(real_assets_dir / filename)
