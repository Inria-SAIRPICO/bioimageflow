"""Focused methods extracted from the execution engine."""

# Pyright checks the complete contract on DefaultEngine; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from bioimageflow_core.worker_protocol import ConsumedRow, OutputGroup

from bioimageflow.row_relation import AssembledResult, ResultRelation, RowAssociation

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
        groups: Sequence[OutputGroup],
        aligned_index: list[Any],
        tool: ProcessingTool,
        *,
        row_consumption: str,
        node_name: str,
        result_key: str | None,
        input_domain: str | None = None,
        input_domain_kind: str | None = None,
    ) -> AssembledResult:
        """Assemble output rows without conflating row identity and consumption."""
        expected = tuple(ConsumedRow(position, str(index)) for position, index in enumerate(aligned_index))
        if row_consumption == "collective":
            if len(groups) != 1 or groups[0].consumed_rows != expected:
                raise ValueError("A collective result must associate one group with the whole actual input batch.")
            domain = f"aggregate::{node_name}::{result_key or node_name}"
            domain_kind = "aggregate"
        elif row_consumption == "mapped":
            if len(groups) != len(expected) or any(
                group.consumed_rows != (row,) for group, row in zip(groups, expected, strict=True)
            ):
                raise ValueError("Mapped result groups must match the exact ordered input rows.")
            domain = input_domain or f"source::{node_name}::{result_key or node_name}"
            domain_kind = input_domain_kind or "source"
        else:
            raise ValueError("Unknown processing row consumption.")

        expanded: list[tuple[str, dict[str, Any]]] = []
        associations: list[RowAssociation] = []
        for group in groups:
            indices: list[str] = []
            for ordinal, output in enumerate(group.outputs):
                if row_consumption == "collective":
                    index = f"{domain}::{ordinal}"
                else:
                    parent = group.consumed_rows[0].row_index
                    index = parent if len(group.outputs) == 1 else f"{parent}::{ordinal}"
                indices.append(index)
                expanded.append((index, self._outputs_to_dict(output)))
            associations.append(RowAssociation(group.consumed_rows, tuple(indices)))

        if expanded:
            dataframe = pd.DataFrame([values for _, values in expanded], index=pd.Index([index for index, _ in expanded]))
        else:
            assert tool.Outputs is not None
            dataframe = pd.DataFrame(columns=pd.Index(list(tool.Outputs._get_all_annotations())))
        relation = ResultRelation(row_consumption, domain, domain_kind, tuple(associations))
        return AssembledResult(dataframe, relation)

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
            if df.index.equals(finest_index):
                aligned.append(df)
                continue
            # Select each column independently: a mixed numeric Series would
            # round large integers and rebuilding rows repeats pandas work.
            if not df.index.is_unique:
                raise IndexAlignmentError("Cannot align a table with duplicate row identities.")
            positions_by_index = {str(index): position for position, index in enumerate(df.index)}
            available = set(positions_by_index)
            positions: list[int] = []
            indices: list[Any] = []
            for index in finest_index:
                source = str(index) if str(index) in available else self._find_parent_index(index, available)
                if source is not None:
                    positions.append(positions_by_index[source])
                    indices.append(index)
            if positions:
                values = pd.DataFrame(df, copy=False)
                expanded = pd.DataFrame(
                    {position: values.iloc[:, position].array.take(positions) for position in range(len(df.columns))},
                    index=pd.Index(indices),
                )
                expanded.columns = df.columns
                # No attrs authority/copy: scientific values (including bound
                # SharedArray references) retain their own identities.
                aligned.append(expanded)
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

        upstream_list = list(upstream_nodes.values())
        relations = [self._result_relation(upstream) for upstream in upstream_list]
        if any(relation is not None and relation.domain_kind == "aggregate" for relation in relations):
            domains = {relation.output_domain for relation in relations if relation is not None}
            if any(relation is None for relation in relations) or len(domains) != 1:
                raise IndexAlignmentError(
                    "Aggregate output rows have an independent domain. Insert an explicit merge DataFrameTool "
                    "(e.g., CrossJoin) to combine aggregate/model outputs with observation rows."
                )
        elif len(upstream_list) > 1:
            lineage_cache: dict[str, set[str]] = {}
            for upstream in upstream_list:
                self._compute_lineage(upstream, lineage_cache, results)
            common_roots = set.intersection(*(lineage_cache[upstream.name] for upstream in upstream_list))
            if not common_roots:
                raise IndexAlignmentError(
                    f"Index alignment error: upstream nodes {[node.name for node in upstream_list]} "
                    "have no common lineage. Insert a merge DataFrameTool (e.g., CrossJoin) to combine them."
                )

        def _max_depth(idx_set: set[Any]) -> int:
            return max((str(i).count("::") for i in idx_set), default=0)

        all_indices = [results[upstream].index for upstream in upstream_list]
        if any(len(index) == 0 for index in all_indices):
            return [], {upstream.name: results[upstream] for upstream in upstream_list}
        finest_index = max(all_indices, key=lambda index: (_max_depth(set(index)), len(index)))
        return list(finest_index), {upstream.name: results[upstream] for upstream in upstream_list}

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
        if isinstance(outputs, dict):
            return {key: str(value) if isinstance(value, Path) else value for key, value in outputs.items()}
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
