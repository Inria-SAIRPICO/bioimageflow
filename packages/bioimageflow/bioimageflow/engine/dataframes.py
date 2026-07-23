"""Focused methods extracted from the execution engine."""

# Pyright checks the complete contract on DefaultEngine; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import (
    Any,
    IndexAlignmentError,
    Node,
    Path,
    ProcessingTool,
    cast,
    pd,
)

if TYPE_CHECKING:
    from bioimageflow.workflow_node import WorkflowNode


class _DataframesMixin:
    def _build_output_dataframe(
        self,
        raw_results: list[list[Any]],
        aligned_index: list[Any],
        tool: ProcessingTool,
    ) -> pd.DataFrame:
        """Build output DataFrame from tool results with index explosion."""
        expanded: list[tuple[str, dict[str, Any]]] = []
        for i, row_outputs in enumerate(raw_results):
            parent_idx = aligned_index[i]
            if len(row_outputs) == 1:
                expanded.append(
                    (str(parent_idx), self._outputs_to_dict(row_outputs[0]))
                )
            else:
                for j, output in enumerate(row_outputs):
                    expanded.append(
                        (f"{parent_idx}::{j}", self._outputs_to_dict(output))
                    )

        if expanded:
            df = pd.DataFrame(
                [d for _, d in expanded],
                index=pd.Index([idx for idx, _ in expanded]),
            )
        else:
            assert tool.Outputs is not None
            output_annotations = tool.Outputs._get_all_annotations()
            df = pd.DataFrame(columns=pd.Index(list(output_annotations.keys())))

        df.index = df.index.astype(str)
        return df

    # ── Recursive workflow execution ───────────────────────────────────

    def _execute_workflow_node(
        self,
        node: "WorkflowNode",
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str | None],
        workflow: Any,
    ) -> tuple[pd.DataFrame, None]:
        """Assemble a compiled workflow boundary after its tools complete."""
        del sig_hashes, workflow
        output_df = self._assemble_workflow_output(node, results)
        return output_df, None

    def _assemble_workflow_output(
        self,
        node: "WorkflowNode",
        results: dict[Node, pd.DataFrame],
    ) -> pd.DataFrame:
        """Assemble the workflow boundary's published output columns."""
        output_frames: list[pd.DataFrame] = []

        for field, col_ref in node._published_outputs.items():
            if col_ref.node not in results:
                raise RuntimeError(
                    f"Internal node '{col_ref.node.name}' not executed — "
                    f"cannot assemble Workflow output."
                )
            df = results[col_ref.node]
            series = cast(pd.Series, df[self._column_label(col_ref)])
            output_frames.append(series.rename(field).to_frame())

        if not output_frames:
            return pd.DataFrame()

        aligned = self._align_dataframes_for_merge(output_frames)
        reference_index = aligned[0].index
        if any(not frame.index.equals(reference_index) for frame in aligned[1:]):
            indexes = [list(frame.index) for frame in aligned]
            raise IndexAlignmentError(
                f"Published workflow outputs have incompatible indexes: {indexes}."
            )
        output_df = pd.concat(aligned, axis=1)
        output_df.index = output_df.index.astype(str)
        return output_df

    # ── Index alignment ────────────────────────────────────────────────

    def _align_dataframes_for_merge(
        self, dfs: list[pd.DataFrame]
    ) -> list[pd.DataFrame]:
        """Align DataFrames with different index granularity for merge.

        Uses ``::`` depth to determine the finest-grained index rather than
        row count, which is correct when some DataFrames have fewer rows due
        to filtering rather than coarser granularity.
        """
        if len(dfs) <= 1:
            return dfs

        def _max_depth(index: pd.Index) -> int:
            return max((str(i).count("::") for i in index), default=0)

        finest_idx = max(
            range(len(dfs)), key=lambda i: (_max_depth(dfs[i].index), len(dfs[i]))
        )
        finest_index = dfs[finest_idx].index

        aligned: list[pd.DataFrame] = []
        for i, df in enumerate(dfs):
            if i == finest_idx:
                aligned.append(df)
                continue
            if set(df.index) == set(finest_index):
                aligned.append(df)
                continue
            # Parent-index expansion
            df_idx_set = set(str(j) for j in df.index)
            expanded_rows: list[Any] = []
            expanded_indices: list[Any] = []
            for idx in finest_index:
                if idx in df_idx_set:
                    expanded_rows.append(df.loc[idx])
                    expanded_indices.append(idx)
                else:
                    parent = self._find_parent_index(idx, df_idx_set)
                    if parent is not None:
                        expanded_rows.append(df.loc[parent])
                        expanded_indices.append(idx)
            if expanded_rows:
                expanded_df = pd.DataFrame(
                    expanded_rows, index=pd.Index(expanded_indices)
                )
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

        lineage_cache: dict[str, set[str]] = {}
        for up_node in upstream_nodes.values():
            self._compute_lineage(up_node, lineage_cache, results)

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

        def _max_depth(idx_set: set[Any]) -> int:
            return max((str(i).count("::") for i in idx_set), default=0)

        all_indices = [set(results[n].index) for n in upstream_nodes.values()]
        if any(not indices for indices in all_indices):
            return [], {n.name: results[n] for n in upstream_nodes.values()}
        finest_index = max(all_indices, key=lambda s: (_max_depth(s), len(s)))
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

        all_upstream: set[Node] = set(node._upstream_nodes)
        for arg in node._args:
            if isinstance(arg, Node):
                all_upstream.add(arg)

        if not all_upstream:
            cache[node.name] = {node.name}
            return cache[node.name]

        lineage: set[str] = set()
        for up in all_upstream:
            lineage |= self._compute_lineage(up, cache, results)

        cache[node.name] = lineage
        return lineage

    # ── Utility helpers ────────────────────────────────────────────────

    def _find_parent_index(self, idx: Any, available_indices: Any) -> str | None:
        """Find the parent index by stripping :: levels progressively.

        *available_indices* may be a ``set`` for O(1) lookup or a pandas
        Index (O(n) per ``in`` check).  Callers on hot paths should pass a
        ``set`` for performance.
        """
        idx_str = str(idx)
        if idx_str in available_indices:
            return idx_str
        while "::" in idx_str:
            idx_str = idx_str.rsplit("::", 1)[0]
            if idx_str in available_indices:
                return idx_str
        return None

    def _outputs_to_dict(self, outputs: Any) -> dict[str, Any]:
        """Convert an Outputs instance to a dict."""
        if hasattr(outputs, "_get_all_annotations"):
            d: dict[str, Any] = {}
            for k in outputs._get_all_annotations():
                v = getattr(outputs, k)
                if isinstance(v, Path):
                    v = str(v)
                d[k] = v
            return d
        return {
            k: str(v) if isinstance(v, Path) else v for k, v in vars(outputs).items()
        }

    def _coerce_numeric_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Convert string columns that look numeric to numeric dtype."""
        for col in df.columns:
            if pd.api.types.is_string_dtype(df[col]):
                try:
                    df[col] = pd.to_numeric(df[col])
                except (ValueError, TypeError):
                    pass
        return df
