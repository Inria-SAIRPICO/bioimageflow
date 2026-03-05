"""Sequential execution engine."""

import time
from pathlib import Path
from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd

from bioimageflow_core.arguments import Arguments, parent_index as get_parent_index
from bioimageflow_core.tool import ProcessingTool
from bioimageflow_core.types import SharedArray
from bioimageflow_core.environment import EnvironmentMismatchError
from bioimageflow.cache import (
    compute_env_hash,
    compute_signature_hash,
    cache_lookup,
    cache_save,
    cache_load,
    cleanup_cache,
    deterministic_serialize,
)
from bioimageflow.node import IndexAlignmentError, Node
from bioimageflow.storage import get_node_dir, get_hash_dir, get_assets_dir, ensure_dirs
from bioimageflow.template import get_output_templates, resolve_template
from bioimageflow.validation import get_tool_version, get_source_hash, is_path_type


class SequentialEngine:
    """Executes workflow nodes sequentially."""

    def execute(self, targets: list[Node], workflow: Any) -> dict[str, pd.DataFrame]:
        """Execute the workflow, returning results for target nodes."""
        # Collect all reachable nodes
        reachable: set[Node] = set()
        for target in targets:
            self._collect_reachable(target, reachable)

        # Check for environment mismatches
        self._check_env_mismatches(reachable)

        # Topological sort
        order = self._topological_sort(reachable)

        # Execute in order
        results: dict[Node, pd.DataFrame] = {}
        sig_hashes: dict[Node, str] = {}

        for node in order:
            df, sig_hash = self._execute_node(node, results, sig_hashes, workflow)
            results[node] = df
            sig_hashes[node] = sig_hash

        return {t.name: results[t] for t in targets}

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

    def _check_env_mismatches(self, nodes: set[Node]) -> None:
        """Check for environment name conflicts with different dependencies."""
        env_specs: dict[str, Any] = {}
        for node in nodes:
            if isinstance(node.tool, ProcessingTool) and hasattr(node.tool, 'environment'):
                env = node.tool.environment
                if env.name in env_specs:
                    existing = env_specs[env.name]
                    if existing.dependencies != env.dependencies:
                        raise EnvironmentMismatchError(
                            f"Environment mismatch for '{env.name}': "
                            f"expected dependencies {existing.dependencies}, "
                            f"but found {env.dependencies}."
                        )
                else:
                    env_specs[env.name] = env

    def _topological_sort(self, nodes: set[Node]) -> list[Node]:
        """Topological sort of reachable nodes. Detects cycles."""
        # Build adjacency: node -> set of upstream nodes (within reachable set)
        in_degree: dict[Node, int] = {n: 0 for n in nodes}
        downstream: dict[Node, set[Node]] = defaultdict(set)

        for node in nodes:
            all_upstream: set[Node] = set(node._upstream_nodes)
            for arg in node._args:
                if isinstance(arg, Node):
                    all_upstream.add(arg)
            for up in all_upstream:
                if up in nodes:
                    in_degree[node] += 1
                    downstream[up].add(node)

        # Kahn's algorithm
        queue = [n for n in nodes if in_degree[n] == 0]
        order: list[Node] = []

        while queue:
            node = queue.pop(0)
            order.append(node)
            for child in downstream[node]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        if len(order) != len(nodes):
            raise RuntimeError(
                "Cycle detected in the DAG. The workflow graph must be acyclic."
            )

        return order

    def _execute_node(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str],
        workflow: Any,
    ) -> tuple[pd.DataFrame, str]:
        """Execute a single node, return (DataFrame, signature_hash)."""
        from bioimageflow.dataframe_tool import DataFrameTool

        is_df_tool = isinstance(node.tool, DataFrameTool)
        is_proc_tool = isinstance(node.tool, ProcessingTool)

        if is_df_tool:
            return self._execute_dataframe_tool(node, results, sig_hashes, workflow)
        elif is_proc_tool:
            return self._execute_processing_tool(node, results, sig_hashes, workflow)
        else:
            raise RuntimeError(f"Unknown tool type: {type(node.tool)}")

    def _execute_dataframe_tool(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str],
        workflow: Any,
    ) -> tuple[pd.DataFrame, str]:
        """Execute a DataFrameTool node."""
        from bioimageflow.dataframe_tool import DataFrameTool
        assert isinstance(node.tool, DataFrameTool)
        # Collect upstream DataFrames from positional args
        dfs: list[pd.DataFrame] = []
        for arg in node._args:
            if isinstance(arg, Node) and arg in results:
                dfs.append(results[arg])

        # Resolve arguments (constants)
        args_dict = dict(node._constant_bindings)
        # Add defaults from Inputs
        for field_name in node.tool.Inputs._get_all_annotations():
            if field_name not in args_dict and hasattr(node.tool.Inputs, field_name):
                args_dict[field_name] = getattr(node.tool.Inputs, field_name)
        arguments = Arguments(**args_dict)

        # Compute signature hash
        env_hash = ""
        tool_version = get_tool_version(node.tool)
        upstream_hashes: dict[str, str] = {}
        for arg in node._args:
            if isinstance(arg, Node) and arg in sig_hashes:
                upstream_hashes[arg.name] = sig_hashes[arg]

        source_hash: str | None = None
        if workflow._dev_mode:
            source_hash = get_source_hash(type(node.tool))

        sig_hash = compute_signature_hash(
            node.tool.name, tool_version, env_hash, args_dict, upstream_hashes,
            source_hash=source_hash,
        )

        # Cache check
        node_dir = get_node_dir(workflow.storage_path, node.name)
        cached = cache_lookup(node_dir, sig_hash)
        if cached:
            self._emit_progress(workflow, node.name, "cached")
            df = cache_load(cached)
            return df, sig_hash

        # Execute
        self._emit_progress(workflow, node.name, "started")
        # Align indices across DataFrames before merge (parent-index expansion)
        if len(dfs) > 1:
            dfs = self._align_dataframes_for_merge(dfs)
        merged = node.tool.merge_dataframes(dfs, arguments)
        # Convert numeric-looking string columns before transform
        merged = self._coerce_numeric_columns(merged)
        df = node.tool.transform(merged, arguments)
        df = self._coerce_numeric_columns(df)

        # Ensure string index
        df.index = df.index.astype(str)

        self._emit_progress(workflow, node.name, "completed")

        # Save to cache
        cache_save(node_dir, sig_hash, df, metadata={
            "tool": node.tool.name,
            "timestamp": time.time(),
        }, parameters=args_dict)
        cleanup_cache(node_dir, workflow.max_executions, workflow.max_age)

        return df, sig_hash

    def _execute_processing_tool(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str],
        workflow: Any,
    ) -> tuple[pd.DataFrame, str]:
        """Execute a ProcessingTool node."""
        assert isinstance(node.tool, ProcessingTool)
        # Determine if this is a source node (no column bindings)
        is_source = len(node._column_bindings) == 0

        if is_source:
            return self._execute_source_processing_tool(node, results, sig_hashes, workflow)

        # Collect upstream nodes from column bindings
        upstream_nodes: dict[str, Node] = {}
        for field, col_ref in node._column_bindings.items():
            upstream_nodes[col_ref.node.name] = col_ref.node

        # Index alignment
        aligned_index, upstream_dfs = self._align_indices(
            node, upstream_nodes, results
        )

        # Value resolution + output templating
        input_annotations = node.tool.Inputs._get_all_annotations()
        templates = get_output_templates(node.tool.Outputs, node.tool.Inputs)

        # Count path-typed input fields for {ext}
        path_input_fields = [
            name for name, ann in input_annotations.items()
            if is_path_type(ann)
        ]

        # Validate that all referenced columns exist in upstream DataFrames
        for field, col_ref in node._column_bindings.items():
            up_df = results[col_ref.node]
            if col_ref.column not in up_df.columns:
                from bioimageflow.node import ColumnNotFoundError
                raise ColumnNotFoundError(
                    f"Column '{col_ref.column}' not found in output of node "
                    f"'{col_ref.node.name}'. Available columns: "
                    f"{list(up_df.columns)}"
                )

        # Resolve per-row arguments
        arguments_dicts: list[dict[str, Any]] = []
        timestamp = str(int(time.time()))

        for i, idx in enumerate(aligned_index):
            row_args: dict[str, Any] = {}

            # Resolve column bindings
            for field, col_ref in node._column_bindings.items():
                up_df = results[col_ref.node]
                if idx in up_df.index:
                    row_args[field] = up_df.at[idx, col_ref.column]
                else:
                    # Try parent index expansion
                    parent_idx = self._find_parent_index(idx, up_df.index)
                    if parent_idx is not None:
                        row_args[field] = up_df.at[parent_idx, col_ref.column]
                    else:
                        raise KeyError(
                            f"Column '{col_ref.column}' not found for index '{idx}' "
                            f"in node '{col_ref.node.name}'"
                        )

            # Add constant bindings
            row_args.update(node._constant_bindings)

            # Add defaults
            for field_name in input_annotations:
                if field_name not in row_args and hasattr(node.tool.Inputs, field_name):
                    row_args[field_name] = getattr(node.tool.Inputs, field_name)

            # Build template context
            context: dict[str, Any] = {
                'node_name': node.name,
                'row_index': str(idx),
                'timestamp': timestamp,
            }

            # Add input field values for .stem/.name/.ext
            for field_name, value in row_args.items():
                context[field_name] = value

            # Add _ext (single path input -> its extension)
            if len(path_input_fields) == 1:
                pf = path_input_fields[0]
                if pf in row_args:
                    context['_ext'] = Path(str(row_args[pf])).suffix
            else:
                context['_ext'] = ''

            # Collect all upstream column values for {column:<name>}
            columns: dict[str, Any] = {}
            for up_name, up_node in upstream_nodes.items():
                up_df = results[up_node]
                if idx in up_df.index:
                    for col in up_df.columns:
                        columns[col] = up_df.at[idx, col]
                else:
                    parent_idx = self._find_parent_index(idx, up_df.index)
                    if parent_idx is not None:
                        for col in up_df.columns:
                            columns[col] = up_df.at[parent_idx, col]
            context['_columns'] = columns

            # Resolve output templates
            assets_dir = get_assets_dir(
                get_hash_dir(get_node_dir(workflow.storage_path, node.name), "pending")
            )
            for out_field, template in templates.items():
                resolved = resolve_template(template, context)
                row_args[out_field] = str(assets_dir / resolved)

            arguments_dicts.append(row_args)

        # Compute signature hash
        env_hash = compute_env_hash(node.tool.environment.dependencies)
        tool_version = get_tool_version(node.tool)
        upstream_hash_map: dict[str, str] = {}
        for up_node in upstream_nodes.values():
            upstream_hash_map[up_node.name] = sig_hashes.get(up_node, "")
        # Also include constants in resolved params
        resolved_params: dict[str, Any] = {
            'bindings': {f: {'node': cr.node.name, 'column': cr.column}
                        for f, cr in node._column_bindings.items()},
            'constants': node._constant_bindings,
            'defaults': {f: getattr(node.tool.Inputs, f)
                        for f in input_annotations
                        if f not in node._column_bindings
                        and f not in node._constant_bindings
                        and hasattr(node.tool.Inputs, f)},
        }

        source_hash: str | None = None
        if workflow._dev_mode:
            source_hash = get_source_hash(type(node.tool))

        sig_hash = compute_signature_hash(
            node.tool.name, tool_version, env_hash, resolved_params,
            upstream_hash_map, source_hash=source_hash,
        )

        # Cache check
        node_dir = get_node_dir(workflow.storage_path, node.name)
        cached = cache_lookup(node_dir, sig_hash)
        if cached:
            self._emit_progress(workflow, node.name, "cached")
            df = cache_load(cached)
            # Restore SharedArray from disk if needed
            df = self._restore_shared_arrays(df, get_hash_dir(node_dir, sig_hash))
            return df, sig_hash

        # Execute
        self._emit_progress(workflow, node.name, "started")

        # Update assets dir with real sig_hash
        real_hash_dir = get_hash_dir(node_dir, sig_hash)
        real_assets_dir = get_assets_dir(real_hash_dir)
        ensure_dirs(real_hash_dir)

        # Fix paths in arguments to use real hash dir
        for row_args in arguments_dicts:
            for out_field in templates:
                if out_field in row_args:
                    old_path = row_args[out_field]
                    filename = Path(old_path).name
                    row_args[out_field] = str(real_assets_dir / filename)

        # Dispatch
        has_batch = type(node.tool).process_batch is not ProcessingTool.process_batch

        if has_batch:
            args_list = [Arguments(**d) for d in arguments_dicts]
            raw_results = node.tool.process_batch(args_list)
            # Auto-wrap list[Outputs] -> list[list[Outputs]]
            if raw_results and not isinstance(raw_results[0], list):
                raw_results = [[r] for r in raw_results]
        else:
            raw_results = []
            for i, args_dict in enumerate(arguments_dicts):
                args = Arguments(**args_dict)
                result = node.tool.process_row(args)
                if not isinstance(result, list):
                    result = [result]
                raw_results.append(result)
                self._emit_progress(workflow, node.name, "row_complete",
                                   row=i, total_rows=len(arguments_dicts))

        # Build output DataFrame
        expanded: list[tuple[str, dict[str, Any]]] = []
        for i, row_outputs in enumerate(raw_results):
            parent_idx = aligned_index[i]
            if len(row_outputs) == 1:
                out_dict = self._outputs_to_dict(row_outputs[0])
                expanded.append((str(parent_idx), out_dict))
            else:
                for j, output in enumerate(row_outputs):
                    out_dict = self._outputs_to_dict(output)
                    expanded.append((f"{parent_idx}::{j}", out_dict))

        if expanded:
            indices = [idx for idx, _ in expanded]
            data = [d for _, d in expanded]
            df = pd.DataFrame(data, index=indices)
        else:
            output_annotations = node.tool.Outputs._get_all_annotations()
            df = pd.DataFrame(columns=list(output_annotations.keys()))

        df.index = df.index.astype(str)
        self._emit_progress(workflow, node.name, "completed")

        # Handle SharedArray caching
        df = self._persist_shared_arrays(df, real_hash_dir)

        # Save to cache
        cache_save(node_dir, sig_hash, df, metadata={
            "tool": node.tool.name,
            "timestamp": time.time(),
        })
        cleanup_cache(node_dir, workflow.max_executions, workflow.max_age)

        return df, sig_hash

    def _execute_source_processing_tool(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str],
        workflow: Any,
    ) -> tuple[pd.DataFrame, str]:
        """Execute a ProcessingTool as a source node (no upstream column refs)."""
        assert isinstance(node.tool, ProcessingTool)
        input_annotations = node.tool.Inputs._get_all_annotations()
        templates = get_output_templates(node.tool.Outputs, node.tool.Inputs)

        # Build single-row arguments from constants and defaults
        row_args: dict[str, Any] = dict(node._constant_bindings)
        for field_name in input_annotations:
            if field_name not in row_args and hasattr(node.tool.Inputs, field_name):
                row_args[field_name] = getattr(node.tool.Inputs, field_name)

        # Compute signature hash
        env_hash = compute_env_hash(node.tool.environment.dependencies)
        tool_version = get_tool_version(node.tool)

        source_hash: str | None = None
        if workflow._dev_mode:
            source_hash = get_source_hash(type(node.tool))

        sig_hash = compute_signature_hash(
            node.tool.name, tool_version, env_hash,
            {'constants': node._constant_bindings},
            {}, source_hash=source_hash,
        )

        # Cache check
        node_dir = get_node_dir(workflow.storage_path, node.name)
        cached = cache_lookup(node_dir, sig_hash)
        if cached:
            self._emit_progress(workflow, node.name, "cached")
            df = cache_load(cached)
            return df, sig_hash

        # Execute
        self._emit_progress(workflow, node.name, "started")

        real_hash_dir = get_hash_dir(node_dir, sig_hash)
        real_assets_dir = get_assets_dir(real_hash_dir)
        ensure_dirs(real_hash_dir)

        # Add template-resolved paths
        timestamp = str(int(time.time()))
        context: dict[str, Any] = {
            'node_name': node.name,
            'row_index': '0',
            'timestamp': timestamp,
            '_ext': '',
            '_columns': {},
        }
        for field_name, value in row_args.items():
            context[field_name] = value

        for out_field, template in templates.items():
            resolved = resolve_template(template, context)
            row_args[out_field] = str(real_assets_dir / resolved)

        args = Arguments(**row_args)

        has_batch = type(node.tool).process_batch is not ProcessingTool.process_batch
        if has_batch:
            raw_results = node.tool.process_batch([args])
            if raw_results and not isinstance(raw_results[0], list):
                raw_results = [[r] for r in raw_results]
            all_outputs: list[Any] = raw_results[0] if raw_results else []
        else:
            result = node.tool.process_row(args)
            if not isinstance(result, list):
                all_outputs = [result]
            else:
                all_outputs = result

        self._emit_progress(workflow, node.name, "row_complete", row=0, total_rows=1)

        # Build output DataFrame
        if len(all_outputs) == 1:
            out_dict = self._outputs_to_dict(all_outputs[0])
            df = pd.DataFrame([out_dict], index=["0"])
        else:
            data: list[dict[str, Any]] = []
            indices: list[str] = []
            for j, output in enumerate(all_outputs):
                out_dict = self._outputs_to_dict(output)
                data.append(out_dict)
                indices.append(f"0::{j}")
            df = pd.DataFrame(data, index=indices)

        df.index = df.index.astype(str)
        self._emit_progress(workflow, node.name, "completed")

        cache_save(node_dir, sig_hash, df, metadata={
            "tool": node.tool.name,
            "timestamp": time.time(),
        })
        cleanup_cache(node_dir, workflow.max_executions, workflow.max_age)

        return df, sig_hash

    def _align_dataframes_for_merge(self, dfs: list[pd.DataFrame]) -> list[pd.DataFrame]:
        """Align DataFrames with different index granularity for merge."""
        if len(dfs) <= 1:
            return dfs

        # Find the finest-grained index (most entries)
        finest_idx = max(range(len(dfs)), key=lambda i: len(dfs[i]))
        finest_index = dfs[finest_idx].index

        aligned: list[pd.DataFrame] = []
        for i, df in enumerate(dfs):
            if i == finest_idx:
                aligned.append(df)
                continue
            # Check if we need to expand
            if set(df.index) == set(finest_index):
                aligned.append(df)
                continue
            # Try parent-index expansion
            expanded_rows: list[Any] = []
            expanded_indices: list[Any] = []
            for idx in finest_index:
                if idx in df.index:
                    expanded_rows.append(df.loc[idx])
                    expanded_indices.append(idx)
                else:
                    parent = self._find_parent_index(idx, df.index)
                    if parent is not None:
                        expanded_rows.append(df.loc[parent])
                        expanded_indices.append(idx)
            if expanded_rows:
                expanded_df = pd.DataFrame(expanded_rows, index=expanded_indices)
                expanded_df.columns = df.columns
                aligned.append(expanded_df)
            else:
                aligned.append(df)

        return aligned

    def _align_indices(
        self,
        node: Node,
        upstream_nodes: dict[str, Node],
        results: dict[Node, pd.DataFrame],
    ) -> tuple[list[Any], dict[str, pd.DataFrame]]:
        """Align indices from multiple upstream nodes."""
        if not upstream_nodes:
            return [], {}

        # Get lineage roots for each upstream node
        lineage_cache: dict[str, set[str]] = {}
        for up_node in upstream_nodes.values():
            self._compute_lineage(up_node, lineage_cache, results)

        # Check all upstreams share at least one common lineage root
        upstream_list = list(upstream_nodes.values())
        if len(upstream_list) > 1:
            common_roots: set[str] | None = None
            for up_node in upstream_list:
                roots = lineage_cache.get(up_node.name, {up_node.name})
                if common_roots is None:
                    common_roots = roots
                else:
                    common_roots = common_roots & roots
            if not common_roots:
                raise IndexAlignmentError(
                    f"Index alignment error: upstream nodes "
                    f"{[n.name for n in upstream_list]} have no common lineage. "
                    f"Insert a merge DataFrameTool (e.g., CrossJoin) to combine them."
                )

        # Find the finest-grained index
        all_indices: list[set[Any]] = []
        for up_node in upstream_nodes.values():
            up_df = results[up_node]
            all_indices.append(set(up_df.index))

        # Use the index with the most entries (finest-grained)
        finest_index = max(all_indices, key=len)

        # Sort for determinism
        aligned = sorted(finest_index, key=str)

        return aligned, {n.name: results[n] for n in upstream_nodes.values()}

    def _compute_lineage(
        self,
        node: Node,
        cache: dict[str, set[str]],
        results: dict[Node, pd.DataFrame],
    ) -> set[str]:
        """Compute lineage roots for a node."""
        if node.name in cache:
            return cache[node.name]

        from bioimageflow.dataframe_tool import DataFrameTool

        # Source node
        all_upstream: set[Node] = set(node._upstream_nodes)
        for arg in node._args:
            if isinstance(arg, Node):
                all_upstream.add(arg)

        if not all_upstream:
            cache[node.name] = {node.name}
            return cache[node.name]

        # Union of all upstream lineages
        lineage: set[str] = set()
        for up in all_upstream:
            up_lineage = self._compute_lineage(up, cache, results)
            lineage |= up_lineage

        cache[node.name] = lineage
        return lineage

    def _find_parent_index(self, idx: Any, available_indices: Any) -> str | None:
        """Find the parent index of idx in available_indices by stripping :: levels."""
        idx_str = str(idx)
        # Try exact match first
        if idx_str in available_indices:
            return idx_str
        # Strip :: levels progressively
        while '::' in idx_str:
            idx_str = idx_str.rsplit('::', 1)[0]
            if idx_str in available_indices:
                return idx_str
        return None

    def _outputs_to_dict(self, outputs: Any) -> dict[str, Any]:
        """Convert an Outputs instance to a dict."""
        if hasattr(outputs, '_get_all_annotations'):
            d: dict[str, Any] = {}
            for k in outputs._get_all_annotations():
                v = getattr(outputs, k)
                if isinstance(v, Path):
                    v = str(v)
                d[k] = v
            return d
        return {k: str(v) if isinstance(v, Path) else v for k, v in vars(outputs).items()}

    def _persist_shared_arrays(self, df: pd.DataFrame, hash_dir: Path) -> pd.DataFrame:
        """Save SharedArray columns to .npy files for caching."""
        assets_dir = get_assets_dir(hash_dir)
        for col in df.columns:
            for idx in df.index:
                val = df.at[idx, col]
                if isinstance(val, SharedArray):
                    try:
                        from bioimageflow_core.shm import open_shared_array
                        npy_path = assets_dir / f"shm_{col}_{idx}.npy"
                        with open_shared_array(val) as arr:
                            np.save(str(npy_path), arr)
                    except Exception:
                        pass
        return df

    def _restore_shared_arrays(self, df: pd.DataFrame, hash_dir: Path) -> pd.DataFrame:
        """Restore SharedArray columns from .npy files on cache load."""
        assets_dir = get_assets_dir(hash_dir)
        for col in df.columns:
            for idx in df.index:
                val = df.at[idx, col]
                if isinstance(val, str):
                    npy_path = assets_dir / f"shm_{col}_{idx}.npy"
                    if npy_path.exists():
                        try:
                            from bioimageflow_core.shm import create_shared_output
                            data = np.load(str(npy_path))
                            # Create new shared memory segment
                            with create_shared_output(data) as ref:
                                df.at[idx, col] = ref
                        except Exception:
                            pass
        return df

    def _coerce_numeric_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Convert string columns that look numeric to numeric dtype."""
        for col in df.columns:
            if pd.api.types.is_string_dtype(df[col]):
                try:
                    df[col] = pd.to_numeric(df[col])
                except (ValueError, TypeError):
                    pass
        return df

    def _emit_progress(
        self,
        workflow: Any,
        node_name: str,
        status: str,
        row: int = 0,
        total_rows: int = 0,
    ) -> None:
        """Emit a progress event."""
        if workflow.on_progress is not None:
            from bioimageflow.workflow import ProgressEvent
            event = ProgressEvent(
                node_name=node_name,
                status=status,
                row=row,
                total_rows=total_rows,
                timestamp=time.time(),
            )
            workflow.on_progress(event)
