"""Execution engines for BioImageFlow workflows."""

import concurrent.futures
import logging
import threading
import time
from pathlib import Path
from collections.abc import Generator
from graphlib import CycleError, TopologicalSorter
from typing import Any, cast, TYPE_CHECKING

import numpy as np
import pandas as pd

from bioimageflow_core.arguments import Arguments
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
)
from bioimageflow.node import IndexAlignmentError, Node
from bioimageflow.storage import get_node_dir, get_hash_dir, get_assets_dir, find_hash_dir, create_hash_dir
from bioimageflow.template import get_output_templates, resolve_template
from bioimageflow.validation import get_tool_version, get_source_hash, is_path_type

if TYPE_CHECKING:
    from bioimageflow.sub_workflow import SubWorkflowNode

logger = logging.getLogger("bioimageflow")


def _configure_default_logging() -> None:
    """Attach a StreamHandler to the bioimageflow and wetlands loggers.

    Called once at engine init so that worker output and engine messages
    are visible on stdout by default.  No-op if handlers already exist.
    """
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")

    for name in ("bioimageflow", "wetlands"):
        lg = logging.getLogger(name)
        if not lg.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(fmt)
            lg.addHandler(handler)
            lg.setLevel(logging.INFO)


def _to_python(val: Any) -> Any:
    """Convert numpy scalars to native Python types.

    pandas DataFrames store numeric values as numpy scalars (np.int64,
    np.float64, etc.).  When these are pickled and sent to Wetlands worker
    environments that don't have numpy installed, unpickling fails.
    The ``.item()`` method is the standard way to get the native Python
    equivalent and is available on all numpy scalar types.
    """
    return val.item() if hasattr(val, "item") else val


class DisabledNodeError(Exception):
    """Raised when all requested target nodes are disabled or unreachable."""
    pass


class WorkflowCancelledError(Exception):
    """Raised when a workflow execution is cancelled via ``Workflow.cancel()``."""
    pass


class NodeStep:
    """Handle for a single node in a stepped workflow execution.

    Yielded by :meth:`DefaultEngine.execute_steps`.  The caller may
    optionally call :meth:`prepare` (to warm up the Wetlands environment
    before execution — useful for attaching a debugger) and **must** call
    :meth:`execute` to run the node (or it will auto-execute when the
    generator advances to the next step).
    """

    def __init__(
        self,
        node: Node,
        engine: "DefaultEngine",
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str],
        workflow: Any,
        skipped: bool = False,
    ) -> None:
        self._node = node
        self._engine = engine
        self._results = results
        self._sig_hashes = sig_hashes
        self._workflow = workflow
        self._skipped = skipped
        self._executed = False
        self._df: pd.DataFrame | None = None
        self._sig_hash: str | None = None
        self._cache_checked = False
        self._cached_df: pd.DataFrame | None = None

    @property
    def skipped(self) -> bool:
        """True if the node is disabled or has a disabled upstream."""
        return self._skipped

    @property
    def node_name(self) -> str:
        """Name of the node about to be executed."""
        return self._node.name

    @property
    def tool(self) -> Any:
        """The tool instance associated with this node."""
        return self._node.tool

    @property
    def environment(self) -> Any:
        """The EnvironmentSpec for ProcessingTools, None for DataFrameTools."""
        if isinstance(self._node.tool, ProcessingTool):
            return self._node.tool.environment
        return None

    @property
    def cached(self) -> bool:
        """True if the node's result is already in the cache.

        The first access triggers a signature-hash computation and cache
        lookup; subsequent accesses reuse the result.
        """
        if self._skipped:
            return False
        self._ensure_cache_checked()
        return self._cached_df is not None

    def prepare(self) -> None:
        """Launch the tool's Wetlands environment (ProcessingTool only).

        No-op for DataFrameTools, when Wetlands is disabled, or when the
        node's result is already cached (no environment needed).  After this
        call the environment is running and a debugger can be attached to it
        before :meth:`execute` triggers the actual computation.
        """
        if self.cached:
            return
        if (
            isinstance(self._node.tool, ProcessingTool)
            and self._engine._env_manager is not None
        ):
            max_workers, worker_env = self._engine._resolve_worker_config(
                self._node.tool, self._workflow,
            )
            self._engine._env_manager.get_or_create(
                self._node.tool.environment,
                max_workers=max_workers,
                worker_env=worker_env,
            )

    def execute(self) -> pd.DataFrame:
        """Execute the node and return its output DataFrame.

        Idempotent — calling more than once returns the cached result.
        If the cache was already checked (via :attr:`cached` or
        :meth:`prepare`) and a hit was found, returns it directly
        without re-entering the engine.
        Raises :class:`DisabledNodeError` if the node is skipped.
        """
        if self._skipped:
            raise DisabledNodeError(
                f"Node '{self._node.name}' is disabled and cannot be executed."
            )
        if self._executed:
            assert self._df is not None
            return self._df
        # Reuse cache result if already checked by prepare() / cached
        self._ensure_cache_checked()
        if self._cached_df is not None:
            self._df = self._cached_df
            self._executed = True
            return self._df
        df, sig_hash = self._engine._execute_node(
            self._node, self._results, self._sig_hashes, self._workflow,
        )
        self._df = df
        self._sig_hash = sig_hash
        self._executed = True
        return df

    def _ensure_cache_checked(self) -> None:
        """Compute sig_hash and check the cache (at most once)."""
        if self._cache_checked:
            return
        self._cache_checked = True
        cached_df, sig_hash = self._engine._check_node_cache(
            self._node, self._results, self._sig_hashes, self._workflow,
        )
        self._cached_df = cached_df
        if sig_hash is not None:
            self._sig_hash = sig_hash


class DefaultEngine:
    """Executes workflow nodes with optional parallelism.

    When ``_force_sequential`` is False (default), independent DAG branches
    can execute concurrently and intra-node rows can run in parallel across
    Wetlands workers.  When True (set by :class:`SequentialEngine`), execution
    is strictly sequential — useful for debugging and deterministic reproduction.

    The non-Wetlands ``_dispatch_direct()`` path is a testing/development
    fallback and does not support sub-row progress reporting (Feature 3) or
    cooperative cancellation (Feature 4).
    """

    def __init__(
        self,
        use_wetlands: bool = False,
        wetlands_config: dict[str, Any] | None = None,
        force_sequential: bool = False,
    ) -> None:
        _configure_default_logging()
        self._use_wetlands = use_wetlands
        self._force_sequential = force_sequential
        self._progress_lock = threading.Lock()
        self._env_manager = None
        if use_wetlands:
            from bioimageflow.env_manager import WetlandsEnvManager
            self._env_manager = WetlandsEnvManager(**(wetlands_config or {}))

    def execute(self, targets: list[Node], workflow: Any) -> dict[str, pd.DataFrame]:
        """Execute the workflow, returning results for target nodes."""
        try:
            return self._execute_impl(targets, workflow)
        finally:
            if self._env_manager is not None:
                self._env_manager.shutdown_all()

    def execute_steps(
        self, targets: list[Node], workflow: Any,
    ) -> Generator[NodeStep, None, None]:
        """Yield a :class:`NodeStep` for each node in topological order.

        The engine (and any Wetlands environments) stays alive between yields.
        The caller controls execution via ``step.prepare()`` / ``step.execute()``.
        If ``execute()`` is not called before advancing, the step auto-executes
        to keep the results chain consistent for downstream nodes.

        SubWorkflowNodes are expanded: their internal nodes are yielded
        individually with scoped names (``subworkflow_name/internal_name``).

        Cleanup runs when the generator is exhausted or closed (early ``break``).
        """
        try:
            reachable: set[Node] = set()
            for target in targets:
                self._collect_reachable(target, reachable)

            self._check_env_mismatches(reachable)
            order = self._topological_sort(reachable)
            _executable, skipped = self._filter_executable(order)

            results: dict[Node, pd.DataFrame] = {}
            sig_hashes: dict[Node, str] = {}

            for node in order:
                if workflow.cancel_requested:
                    raise WorkflowCancelledError("Workflow cancelled by user")

                if node in skipped:
                    logger.info(
                        "Skipping node '%s' (disabled or upstream disabled)", node.name
                    )
                    yield NodeStep(node, self, results, sig_hashes, workflow, skipped=True)
                    continue

                from bioimageflow.sub_workflow import SubWorkflowNode
                if isinstance(node, SubWorkflowNode):
                    yield from self._execute_sub_workflow_steps(
                        node, results, sig_hashes, workflow,
                    )
                    continue

                step = NodeStep(node, self, results, sig_hashes, workflow)
                yield step
                # Auto-execute if the user didn't call step.execute()
                if not step._executed:
                    step.execute()
                results[node] = step._df  # type: ignore[assignment]
                sig_hashes[node] = step._sig_hash  # type: ignore[assignment]
        finally:
            if self._env_manager is not None:
                self._env_manager.shutdown_all()

    def _execute_sub_workflow_steps(
        self,
        node: "SubWorkflowNode",
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str],
        workflow: Any,
    ) -> Generator[NodeStep, None, None]:
        """Yield individual steps for a SubWorkflowNode's internal nodes."""
        from bioimageflow.sub_workflow import SubWorkflowNode

        # Build proxy DataFrame
        proxy_df = self._build_proxy_dataframe(node, results)
        proxy_node = node._proxy_node
        results[proxy_node] = proxy_df
        sig_hashes[proxy_node] = "proxy"

        # Sort internal nodes
        internal_nodes = set(node.internal_nodes)
        internal_nodes.add(proxy_node)
        internal_order = self._topological_sort(internal_nodes)

        # Scope names — only direct children, not recursively.
        # Nested sub-workflows get scoped when they are recursively expanded.
        original_names: dict[Node, str] = {}
        for inode in node.internal_nodes:
            original_names[inode] = inode._name
            inode._name = f"{node.name}/{inode._name}"

        try:
            for inode in internal_order:
                if inode is proxy_node:
                    continue

                if isinstance(inode, SubWorkflowNode):
                    yield from self._execute_sub_workflow_steps(
                        inode, results, sig_hashes, workflow,
                    )
                    continue

                step = NodeStep(inode, self, results, sig_hashes, workflow)
                yield step
                if not step._executed:
                    step.execute()
                results[inode] = step._df  # type: ignore[assignment]
                sig_hashes[inode] = step._sig_hash  # type: ignore[assignment]

            # Assemble output
            output_df = self._assemble_sub_workflow_output(node, results)
            import hashlib
            import json
            terminal_hashes = {}
            for field, col_ref in node._output_mapping.items():
                if col_ref.node in sig_hashes:
                    terminal_hashes[field] = sig_hashes[col_ref.node]
            combined = hashlib.sha256(
                json.dumps(terminal_hashes, sort_keys=True).encode()
            ).hexdigest()
            results[node] = output_df
            sig_hashes[node] = combined
        finally:
            for inode, orig_name in original_names.items():
                inode._name = orig_name

    def _execute_impl(self, targets: list[Node], workflow: Any) -> dict[str, pd.DataFrame]:
        """Internal execution logic.

        Uses ``TopologicalSorter.get_ready()`` / ``.done()`` to find
        independent nodes and, when ``_force_sequential`` is ``False``,
        dispatches independent ``ProcessingTool`` nodes concurrently via
        ``ThreadPoolExecutor``.  ``DataFrameTool`` nodes always run on
        the main thread.
        """
        from bioimageflow.dataframe_tool import DataFrameTool

        reachable: set[Node] = set()
        for target in targets:
            self._collect_reachable(target, reachable)

        self._check_env_mismatches(reachable)
        order = self._topological_sort(reachable)
        executable, skipped = self._filter_executable(order)

        # Check that at least one target is executable
        executable_targets = [t for t in targets if t not in skipped]
        if not executable_targets:
            disabled_names = [t.name for t in targets]
            raise DisabledNodeError(
                f"All target nodes are disabled or have disabled "
                f"upstream dependencies: {disabled_names}"
            )

        dep_graph = self._build_dep_graph(executable)
        ts = TopologicalSorter(dep_graph)
        ts.prepare()

        results: dict[Node, pd.DataFrame] = {}
        sig_hashes: dict[Node, str] = {}
        lock = threading.Lock()  # protects results and sig_hashes dict writes

        while ts.is_active():
            if workflow.cancel_requested:
                raise WorkflowCancelledError("Workflow cancelled by user")

            ready = list(ts.get_ready())
            if not ready:
                break

            # Separate DataFrameTool nodes (must run on main thread)
            # from ProcessingTool nodes (can run in threads)
            df_nodes = [n for n in ready if isinstance(n.tool, DataFrameTool)]
            pt_nodes = [n for n in ready if n not in set(df_nodes)]

            # Execute DataFrameTool nodes sequentially on main thread
            for node in df_nodes:
                df, sig_hash = self._execute_node(node, results, sig_hashes, workflow)
                results[node] = df
                sig_hashes[node] = sig_hash
                ts.done(node)

            if len(pt_nodes) <= 1 or self._force_sequential:
                # Single node or forced sequential — no threading overhead
                for node in pt_nodes:
                    if workflow.cancel_requested:
                        raise WorkflowCancelledError("Workflow cancelled by user")
                    df, sig_hash = self._execute_node(node, results, sig_hashes, workflow)
                    results[node] = df
                    sig_hashes[node] = sig_hash
                    ts.done(node)
            else:
                # Multiple independent ProcessingTool nodes — run concurrently.
                # Cap threads to avoid excessive overhead; each thread mostly
                # waits on Wetlands IPC, so the cap is generous.
                pool_size = min(len(pt_nodes), 8)
                with concurrent.futures.ThreadPoolExecutor(max_workers=pool_size) as pool:
                    future_to_node = {
                        pool.submit(self._execute_node, node, results, sig_hashes, workflow): node
                        for node in pt_nodes
                    }
                    for future in concurrent.futures.as_completed(future_to_node):
                        node = future_to_node[future]
                        df, sig_hash = future.result()  # raises on failure
                        with lock:
                            results[node] = df
                            sig_hashes[node] = sig_hash
                        ts.done(node)

        # Log skipped targets
        for t in targets:
            if t in skipped:
                logger.info("Skipping disabled target node '%s'", t.name)

        return {t.name: results[t] for t in targets if t not in skipped}

    # ── Graph traversal ────────────────────────────────────────────────

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

    def _filter_executable(
        self, order: list[Node]
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
            upstream_skipped = any(
                up in skipped for up in node._upstream_nodes
            ) or any(
                arg in skipped
                for arg in node._args
                if isinstance(arg, Node)
            )
            if upstream_skipped:
                skipped.add(node)
                continue
            executable.append(node)
        return executable, skipped

    def _check_env_mismatches(self, nodes: set[Node]) -> None:
        """Check for environment name conflicts with different dependencies."""
        env_specs: dict[str, tuple[Any, str]] = {}  # name -> (env, tool_name)
        for node in nodes:
            if isinstance(node.tool, ProcessingTool) and hasattr(node.tool, 'environment'):
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

    def _topological_sort(self, nodes: set[Node]) -> list[Node]:
        """Topological sort of reachable nodes using graphlib.TopologicalSorter."""
        dep_graph = self._build_dep_graph_from_set(nodes)
        try:
            return list(TopologicalSorter(dep_graph).static_order())
        except CycleError as exc:
            raise RuntimeError(
                f"Cycle detected in the DAG. The workflow graph must be acyclic. "
                f"Cycle info: {exc.args[1]}"
            ) from exc

    def _build_dep_graph_from_set(self, nodes: set[Node]) -> dict[Node, set[Node]]:
        """Build dependency graph from a set of nodes (for topological sort)."""
        dep_graph: dict[Node, set[Node]] = {}
        for node in nodes:
            all_upstream: set[Node] = set(node._upstream_nodes)
            for arg in node._args:
                if isinstance(arg, Node):
                    all_upstream.add(arg)
            dep_graph[node] = {up for up in all_upstream if up in nodes}
        return dep_graph

    def _build_dep_graph(self, executable: list[Node]) -> dict[Node, set[Node]]:
        """Build dependency graph for TopologicalSorter from executable nodes."""
        executable_set = set(executable)
        dep_graph: dict[Node, set[Node]] = {}
        for node in executable:
            deps: set[Node] = set()
            for up in node._upstream_nodes:
                if up in executable_set:
                    deps.add(up)
            for arg in node._args:
                if isinstance(arg, Node) and arg in executable_set:
                    deps.add(arg)
            dep_graph[node] = deps
        return dep_graph

    # ── Cache pre-check ─────────────────────────────────────────────────

    def _check_node_cache(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str],
        workflow: Any,
    ) -> tuple[pd.DataFrame | None, str | None]:
        """Check whether a node's result is already cached.

        Returns ``(cached_df, sig_hash)`` if a cache hit is found, or
        ``(None, sig_hash)`` if computable but not cached.  Returns
        ``(None, None)`` for SubWorkflowNodes (not cacheable at this level).
        """
        from bioimageflow.dataframe_tool import DataFrameTool
        from bioimageflow.sub_workflow import SubWorkflowNode

        if isinstance(node, SubWorkflowNode):
            return None, None

        # ── Compute signature hash ──
        if isinstance(node.tool, DataFrameTool):
            _arguments, args_dict = self._resolve_constant_arguments(node)
            upstream_hashes = {
                arg.name: sig_hashes[arg]
                for arg in node._args
                if isinstance(arg, Node) and arg in sig_hashes
            }
            sig_hash = self._compute_sig_hash(node, "", args_dict, upstream_hashes, workflow)
        elif isinstance(node.tool, ProcessingTool):
            if not node._column_bindings:
                env_hash = compute_env_hash(node.tool.environment.dependencies)
                sig_hash = self._compute_sig_hash(
                    node, env_hash, {'constants': node._constant_bindings}, {}, workflow,
                )
            else:
                input_annotations = node.tool.Inputs._get_all_annotations()
                upstream_nodes = {
                    cr.node.name: cr.node
                    for cr in node._column_bindings.values()
                }
                sig_hash = self._compute_processing_sig_hash(
                    node, input_annotations, upstream_nodes, sig_hashes, workflow,
                )
        else:
            return None, None

        # ── Cache lookup ──
        node_dir = get_node_dir(workflow.storage_path, node.name)
        cached = cache_lookup(node_dir, sig_hash)
        if not cached:
            return None, sig_hash

        df = self._coerce_numeric_columns(cache_load(cached))
        # Restore shared arrays for ProcessingTools with column bindings
        if (
            isinstance(node.tool, ProcessingTool)
            and node._column_bindings
        ):
            cached_hash_dir = find_hash_dir(node_dir, sig_hash)
            if cached_hash_dir is not None:
                df = self._restore_shared_arrays(df, cached_hash_dir)
        return df, sig_hash

    # ── Node dispatch ──────────────────────────────────────────────────

    def _execute_node(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str],
        workflow: Any,
    ) -> tuple[pd.DataFrame, str]:
        """Execute a single node, return (DataFrame, signature_hash)."""
        from bioimageflow.dataframe_tool import DataFrameTool
        from bioimageflow.sub_workflow import SubWorkflowNode

        try:
            if isinstance(node, SubWorkflowNode):
                return self._execute_sub_workflow(node, results, sig_hashes, workflow)
            elif isinstance(node.tool, DataFrameTool):
                return self._execute_dataframe_tool(node, results, sig_hashes, workflow)
            elif isinstance(node.tool, ProcessingTool):
                if not node._column_bindings:
                    return self._execute_source_processing_tool(node, results, sig_hashes, workflow)
                else:
                    return self._execute_processing_tool_with_column_bindings(node, results, sig_hashes, workflow)
            else:
                raise RuntimeError(f"Unknown tool type: {type(node.tool)}")
        except WorkflowCancelledError:
            self._emit_progress(workflow, node.name, "cancelled")
            raise
        except Exception:
            self._emit_progress(workflow, node.name, "failed")
            raise

    # ── DataFrameTool execution ────────────────────────────────────────

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

        dfs = [results[arg] for arg in node._args
               if isinstance(arg, Node) and arg in results]
        arguments, args_dict = self._resolve_constant_arguments(node)

        upstream_hashes = {arg.name: sig_hashes[arg]
                          for arg in node._args
                          if isinstance(arg, Node) and arg in sig_hashes}
        sig_hash = self._compute_sig_hash(node, "", args_dict, upstream_hashes, workflow)

        node_dir = get_node_dir(workflow.storage_path, node.name)
        cached = cache_lookup(node_dir, sig_hash)
        if cached:
            self._emit_progress(workflow, node.name, "cached")
            return self._coerce_numeric_columns(cache_load(cached)), sig_hash

        self._emit_progress(workflow, node.name, "started")

        if len(dfs) > 1:
            dfs = self._align_dataframes_for_merge(dfs)
        merged = node.tool.merge_dataframes(dfs, arguments)
        merged = self._coerce_numeric_columns(merged)
        df = node.tool.transform(merged, arguments)
        df = self._coerce_numeric_columns(df)
        df.index = df.index.astype(str)

        self._emit_progress(workflow, node.name, "completed")
        hash_dir = create_hash_dir(node_dir, sig_hash)
        self._save_and_cleanup(node_dir, sig_hash, df, type(node.tool).__name__,
                               workflow, parameters=args_dict, hash_dir=hash_dir)
        return df, sig_hash

    # ── ProcessingTool execution ───────────────────────────────────────

    def _execute_source_processing_tool(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str],
        workflow: Any,
    ) -> tuple[pd.DataFrame, str]:
        """Execute a ProcessingTool node that has no upstream column bindings (source node)."""
        assert isinstance(node.tool, ProcessingTool)

        input_annotations = node.tool.Inputs._get_all_annotations()
        assert node.tool.Outputs is not None  # ProcessingTool always has Outputs
        templates = get_output_templates(node.tool.Outputs, node.tool.Inputs)

        aligned_index: list[Any] = ["0"]

        # --- Signature hash ---
        env_hash = compute_env_hash(node.tool.environment.dependencies)
        sig_hash = self._compute_sig_hash(
            node, env_hash, {'constants': node._constant_bindings}, {}, workflow,
        )

        # --- Cache check ---
        node_dir = get_node_dir(workflow.storage_path, node.name)
        cached = cache_lookup(node_dir, sig_hash)
        if cached:
            self._emit_progress(workflow, node.name, "cached")
            return self._coerce_numeric_columns(cache_load(cached)), sig_hash

        # --- Resolve arguments ---
        self._emit_progress(workflow, node.name, "started")
        hash_dir, real_assets_dir = self._prepare_output_dir(node_dir, sig_hash)

        row_args = self._resolve_defaults(node, input_annotations)
        context = self._build_template_context(
            node.name, '0', row_args, path_input_fields=[], upstream_nodes={},
            results={}, idx='0',
        )
        for out_field, template in templates.items():
            row_args[out_field] = str(real_assets_dir / resolve_template(template, context))
        arguments_dicts = [row_args]

        # --- Dispatch & build output ---
        raw_results = self._dispatch_tool(node.tool, arguments_dicts, workflow, node.name)
        df = self._build_output_dataframe(raw_results, aligned_index, node.tool)
        self._emit_progress(workflow, node.name, "completed")

        self._save_and_cleanup(node_dir, sig_hash, df, type(node.tool).__name__, workflow,
                               hash_dir=hash_dir)
        return df, sig_hash

    def _execute_processing_tool_with_column_bindings(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str],
        workflow: Any,
    ) -> tuple[pd.DataFrame, str]:
        """Execute a ProcessingTool node that has upstream column bindings."""
        assert isinstance(node.tool, ProcessingTool)

        input_annotations = node.tool.Inputs._get_all_annotations()
        assert node.tool.Outputs is not None  # ProcessingTool always has Outputs
        templates = get_output_templates(node.tool.Outputs, node.tool.Inputs)

        upstream_nodes = {cr.node.name: cr.node
                         for cr in node._column_bindings.values()}
        aligned_index, _ = self._align_indices(node, upstream_nodes, results)
        self._validate_column_bindings(node, results)

        # --- Signature hash ---
        sig_hash = self._compute_processing_sig_hash(
            node, input_annotations, upstream_nodes, sig_hashes, workflow,
        )

        # --- Cache check ---
        node_dir = get_node_dir(workflow.storage_path, node.name)
        cached = cache_lookup(node_dir, sig_hash)
        if cached:
            self._emit_progress(workflow, node.name, "cached")
            df = self._coerce_numeric_columns(cache_load(cached))
            cached_hash_dir = find_hash_dir(node_dir, sig_hash)
            if cached_hash_dir is not None:
                df = self._restore_shared_arrays(df, cached_hash_dir)
            return df, sig_hash

        # --- Resolve arguments ---
        self._emit_progress(workflow, node.name, "started")
        hash_dir, real_assets_dir = self._prepare_output_dir(node_dir, sig_hash)

        path_input_fields = [n for n, a in input_annotations.items() if is_path_type(a)]
        arguments_dicts = self._resolve_all_row_arguments(
            node, aligned_index, results, upstream_nodes,
            input_annotations, templates, path_input_fields, workflow,
        )
        self._fixup_output_paths(arguments_dicts, templates, real_assets_dir)

        # --- Dispatch & build output ---
        raw_results = self._dispatch_tool(node.tool, arguments_dicts, workflow, node.name)
        df = self._build_output_dataframe(raw_results, aligned_index, node.tool)
        self._emit_progress(workflow, node.name, "completed")

        df = self._persist_shared_arrays(df, hash_dir)
        self._save_and_cleanup(node_dir, sig_hash, df, type(node.tool).__name__, workflow,
                               hash_dir=hash_dir)
        return df, sig_hash

    # ── Argument resolution ────────────────────────────────────────────

    def _resolve_constant_arguments(
        self, node: Node,
    ) -> tuple[Arguments, dict[str, Any]]:
        """Resolve constants + defaults into an Arguments object and raw dict."""
        input_annotations = node.tool.Inputs._get_all_annotations()
        args_dict = dict(node._constant_bindings)
        for field_name in input_annotations:
            if field_name not in args_dict and hasattr(node.tool.Inputs, field_name):
                args_dict[field_name] = getattr(node.tool.Inputs, field_name)
        return Arguments(**args_dict), args_dict

    def _resolve_defaults(
        self, node: Node, input_annotations: dict[str, Any],
    ) -> dict[str, Any]:
        """Build args dict from constants and defaults (no column bindings)."""
        row_args = dict(node._constant_bindings)
        for field_name in input_annotations:
            if field_name not in row_args and hasattr(node.tool.Inputs, field_name):
                row_args[field_name] = getattr(node.tool.Inputs, field_name)
        return row_args

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
                node, idx, results, input_annotations, index_sets,
            )
            context = self._build_template_context(
                node.name, str(idx), row_args, path_input_fields,
                upstream_nodes, results, idx, timestamp,
            )
            assets_dir = get_assets_dir(
                get_hash_dir(get_node_dir(workflow.storage_path, node.name), "pending")
            )
            for out_field, template in templates.items():
                row_args[out_field] = str(assets_dir / resolve_template(template, context))

            arguments_dicts.append(row_args)
        return arguments_dicts

    def _resolve_single_row(
        self,
        node: Node,
        idx: Any,
        results: dict[Node, pd.DataFrame],
        input_annotations: dict[str, Any],
        index_sets: dict[str, set[str]] | None = None,
    ) -> dict[str, Any]:
        """Resolve column bindings, constants, and defaults for one row."""
        row_args: dict[str, Any] = {}

        for field, col_ref in node._column_bindings.items():
            up_df = results[col_ref.node]
            idx_set = (index_sets or {}).get(col_ref.node.name) or set(up_df.index)
            if idx in idx_set:
                row_args[field] = _to_python(up_df.at[idx, col_ref.column])
            else:
                parent_idx = self._find_parent_index(idx, idx_set)
                if parent_idx is not None:
                    row_args[field] = _to_python(up_df.at[parent_idx, col_ref.column])
                else:
                    raise KeyError(
                        f"Column '{col_ref.column}' not found for index '{idx}' "
                        f"in node '{col_ref.node.name}'"
                    )

        row_args.update(node._constant_bindings)

        for field_name in input_annotations:
            if field_name not in row_args and hasattr(node.tool.Inputs, field_name):
                row_args[field_name] = getattr(node.tool.Inputs, field_name)

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
            'node_name': node_name,
            'row_index': row_index.replace('::', '_'),
            'timestamp': timestamp or str(int(time.time())),
        }
        for field_name, value in row_args.items():
            context[field_name] = value

        if len(path_input_fields) == 1:
            pf = path_input_fields[0]
            context['_ext'] = Path(str(row_args[pf])).suffix if pf in row_args else ''
        else:
            context['_ext'] = ''

        # Collect upstream column values for {column:<name>}
        columns: dict[str, Any] = {}
        for up_node in upstream_nodes.values():
            up_df = results.get(up_node)
            if up_df is None:
                continue
            idx_set = set(str(i) for i in up_df.index)
            resolved_idx = idx if idx in idx_set else self._find_parent_index(idx, idx_set)
            if resolved_idx is not None:
                for col in up_df.columns:
                    columns[col] = up_df.at[resolved_idx, col]
        context['_columns'] = columns
        return context

    # ── Validation ─────────────────────────────────────────────────────

    def _validate_column_bindings(
        self, node: Node, results: dict[Node, pd.DataFrame],
    ) -> None:
        """Check that all referenced columns exist in upstream DataFrames."""
        for field, col_ref in node._column_bindings.items():
            up_df = results[col_ref.node]
            if col_ref.column not in up_df.columns:
                from bioimageflow.node import ColumnNotFoundError
                raise ColumnNotFoundError(
                    f"Column '{col_ref.column}' not found in output of node "
                    f"'{col_ref.node.name}'. Available columns: "
                    f"{list(up_df.columns)}"
                )

    # ── Signature hashing ──────────────────────────────────────────────

    def _compute_sig_hash(
        self,
        node: Node,
        env_hash: str,
        resolved_params: Any,
        upstream_hashes: dict[str, str],
        workflow: Any,
    ) -> str:
        """Compute signature hash for any node type."""
        tool_version = get_tool_version(node.tool)
        source_hash = get_source_hash(type(node.tool)) if workflow._dev_mode else None
        return compute_signature_hash(
            type(node.tool).__name__, tool_version, env_hash, resolved_params,
            upstream_hashes, source_hash=source_hash,
        )

    def _compute_processing_sig_hash(
        self,
        node: Node,
        input_annotations: dict[str, Any],
        upstream_nodes: dict[str, Node],
        sig_hashes: dict[Node, str],
        workflow: Any,
    ) -> str:
        """Compute signature hash for a non-source ProcessingTool."""
        env_hash = compute_env_hash(cast(ProcessingTool, node.tool).environment.dependencies)
        missing = [n.name for n in upstream_nodes.values() if n not in sig_hashes]
        if missing:
            raise RuntimeError(
                f"Cannot compute signature hash for node: upstream nodes "
                f"{missing} have not been executed yet."
            )
        upstream_hash_map = {n.name: sig_hashes[n]
                            for n in upstream_nodes.values()}
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
        return self._compute_sig_hash(
            node, env_hash, resolved_params, upstream_hash_map, workflow,
        )

    # ── Dispatch & output construction ─────────────────────────────────

    def _prepare_output_dir(self, node_dir: Path, sig_hash: str) -> tuple[Path, Path]:
        """Create a timestamped output directory. Returns (hash_dir, assets_dir)."""
        hash_dir = create_hash_dir(node_dir, sig_hash)
        return hash_dir, get_assets_dir(hash_dir)

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

    def _dispatch_tool(
        self,
        tool: ProcessingTool,
        arguments_dicts: list[dict[str, Any]],
        workflow: Any,
        node_name: str,
    ) -> list[list[Any]]:
        """Dispatch to process_batch or process_row. Returns list[list[Outputs]]."""
        has_batch = type(tool).process_batch is not ProcessingTool.process_batch

        if self._use_wetlands and self._env_manager is not None:
            return self._dispatch_via_wetlands(tool, arguments_dicts, workflow,
                                               node_name, has_batch)

        return self._dispatch_direct(tool, arguments_dicts, workflow,
                                     node_name, has_batch)

    def _dispatch_direct(
        self,
        tool: ProcessingTool,
        arguments_dicts: list[dict[str, Any]],
        workflow: Any,
        node_name: str,
        has_batch: bool,
    ) -> list[list[Any]]:
        """Direct dispatch — tool runs in the main process."""
        if has_batch:
            args_list = [Arguments(**d) for d in arguments_dicts]
            raw_results = tool.process_batch(args_list)
            if raw_results and not isinstance(raw_results[0], list):
                raw_results = [[r] for r in raw_results]
            return raw_results

        raw_results: list[list[Any]] = []
        for i, args_dict in enumerate(arguments_dicts):
            result = tool.process_row(Arguments(**args_dict))
            if not isinstance(result, list):
                result = [result]
            raw_results.append(result)
            self._emit_progress(workflow, node_name, "row_complete",
                                row=i, total_rows=len(arguments_dicts))
        return raw_results

    def _resolve_worker_config(
        self, tool: ProcessingTool, workflow: Any,
    ) -> tuple[int, Any]:
        """Determine max_workers and worker_env for a tool's environment.

        Resolution order:
        1. Explicit ``get_environment()`` override takes precedence.
        2. GPU auto-inference: if any tool in the environment declares
           ``ResourceSpec(gpu >= 1)`` and no explicit ``worker_env`` was set,
           auto-generate ``worker_env = lambda i: {"CUDA_VISIBLE_DEVICES": str(i)}``.
        3. Fall back to ``Workflow.max_workers``, no ``worker_env``.
        """
        env_name = tool.environment.name
        env_config = workflow._env_configs.get(env_name)

        # max_workers: explicit override > workflow default
        if env_config and env_config.max_workers > 0:
            max_workers = env_config.max_workers
        else:
            max_workers = workflow.max_workers

        # worker_env: explicit override > GPU auto-inference > None
        if env_config and env_config.worker_env is not None:
            worker_env = env_config.worker_env
        elif self._env_has_gpu_tool(env_name, workflow):
            worker_env = lambda i: {"CUDA_VISIBLE_DEVICES": str(i)}
        else:
            worker_env = None

        return max_workers, worker_env

    def _env_has_gpu_tool(self, env_name: str, workflow: Any) -> bool:
        """Check if any tool in this workflow sharing this env declares gpu >= 1."""
        for node in workflow._nodes.values():
            tool = node.tool
            if (
                isinstance(tool, ProcessingTool)
                and hasattr(tool, 'environment')
                and tool.environment.name == env_name
                and hasattr(tool, 'resources')
                and tool.resources is not None
                and tool.resources.gpu >= 1
            ):
                return True
        return False

    def _dispatch_via_wetlands(
        self,
        tool: ProcessingTool,
        arguments_dicts: list[dict[str, Any]],
        workflow: Any,
        node_name: str,
        has_batch: bool,
    ) -> list[list[Any]]:
        """Dispatch through Wetlands — tool runs in isolated environment workers."""
        from bioimageflow.env_manager import _find_tool_file
        from wetlands.task import TaskStatus, TaskEventType

        assert self._env_manager is not None
        env_spec = tool.environment
        tool_file_path = _find_tool_file(type(tool))
        tool_class_name = type(tool).__name__
        max_workers, worker_env = self._resolve_worker_config(tool, workflow)

        if has_batch:
            task = self._env_manager.submit_process_batch(
                env_spec, tool_file_path, tool_class_name, arguments_dicts,
                max_workers=max_workers, worker_env=worker_env,
            )
            task.wait_for()
            if task.status == TaskStatus.FAILED:
                raise task.exception
            if task.status == TaskStatus.CANCELED:
                raise WorkflowCancelledError("Workflow cancelled during batch execution")
            result_dicts = task.result
            assert tool.Outputs is not None
            return [[tool.Outputs(**d) for d in row] for row in result_dicts]

        tasks = self._env_manager.map_process_rows(
            env_spec, tool_file_path, tool_class_name, arguments_dicts,
            max_workers=max_workers, worker_env=worker_env,
        )

        # Attach progress listeners for sub-row progress reporting
        for i, task in enumerate(tasks):
            def _make_listener(row_idx):
                def on_event(event):
                    if event.type == TaskEventType.UPDATE:
                        self._emit_progress(
                            workflow, node_name, "row_progress",
                            row=row_idx, total_rows=len(tasks),
                            message=event.task.message,
                            current=event.task.current,
                            maximum=event.task.maximum,
                        )
                return on_event
            task.listen(_make_listener(i))

        # Wait and collect results — fail-fast on first error or cancel
        try:
            for task in tasks:
                if workflow.cancel_requested:
                    raise WorkflowCancelledError("Workflow cancelled by user")
                task.wait_for()
                if task.status == TaskStatus.FAILED:
                    raise task.exception
        except (WorkflowCancelledError, Exception):
            # Cancel all remaining in-flight tasks
            for t in tasks:
                if not t.status.is_finished():
                    t.cancel()
            for t in tasks:
                if not t.status.is_finished():
                    try:
                        t.wait_for(timeout=10)
                    except Exception:
                        pass
            raise

        # Collect results in submission order — skip cancelled tasks
        raw_results: list[list[Any]] = []
        assert tool.Outputs is not None
        for i, task in enumerate(tasks):
            if task.status == TaskStatus.CANCELED:
                continue
            result_dicts = task.result
            raw_results.append([tool.Outputs(**d) for d in result_dicts])
            self._emit_progress(workflow, node_name, "row_complete",
                                row=i, total_rows=len(tasks))
        return raw_results

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
                expanded.append((str(parent_idx), self._outputs_to_dict(row_outputs[0])))
            else:
                for j, output in enumerate(row_outputs):
                    expanded.append((f"{parent_idx}::{j}", self._outputs_to_dict(output)))

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

    # ── Sub-workflow execution ─────────────────────────────────────────

    def _execute_sub_workflow(
        self,
        node: "SubWorkflowNode",
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str],
        workflow: Any,
    ) -> tuple[pd.DataFrame, str]:
        """Execute a SubWorkflowNode by running its internal nodes."""

        # Build a proxy DataFrame from the parent's upstream data.
        # The proxy node should expose columns matching SubWorkflow.Inputs.
        proxy_df = self._build_proxy_dataframe(node, results)
        proxy_node = node._proxy_node
        results[proxy_node] = proxy_df
        sig_hashes[proxy_node] = "proxy"

        # Collect and sort internal nodes
        internal_nodes = set(node.internal_nodes)
        internal_nodes.add(proxy_node)
        order = self._topological_sort(internal_nodes)

        # Execute internal nodes with scoped names for caching/progress.
        # Only scope direct children — nested sub-workflows get scoped
        # when _execute_sub_workflow is called recursively for them.
        original_names: dict[Node, str] = {}
        for inode in node.internal_nodes:
            original_names[inode] = inode._name
            inode._name = f"{node.name}/{inode._name}"

        try:
            for inode in order:
                if inode is proxy_node:
                    continue  # Already have proxy data
                df, sig_hash = self._execute_node(inode, results, sig_hashes, workflow)
                results[inode] = df
                sig_hashes[inode] = sig_hash
        finally:
            # Restore original names
            for inode, orig_name in original_names.items():
                inode._name = orig_name

        # Assemble output DataFrame from output mapping
        output_df = self._assemble_sub_workflow_output(node, results)

        # Compute a combined sig hash from internal terminal hashes
        terminal_hashes = {}
        for field, col_ref in node._output_mapping.items():
            if col_ref.node in sig_hashes:
                terminal_hashes[field] = sig_hashes[col_ref.node]
        import hashlib
        import json
        combined = hashlib.sha256(
            json.dumps(terminal_hashes, sort_keys=True).encode()
        ).hexdigest()

        return output_df, combined

    def _build_proxy_dataframe(
        self,
        node: "SubWorkflowNode",
        results: dict[Node, pd.DataFrame],
    ) -> pd.DataFrame:
        """Build a proxy DataFrame containing only column-bound fields.

        Constants and defaults are NOT included — they flow through the
        internal nodes' ``_constant_bindings`` instead, which keeps them
        as native Python types and avoids numpy coercion by pandas.
        """
        if not node._input_column_bindings:
            # No column bindings at all — return an empty single-row DataFrame
            # so internal nodes still have an index to iterate over.
            return pd.DataFrame(index=pd.Index(["0"]))

        # Collect upstream DataFrames
        upstream_dfs: dict[Node, pd.DataFrame] = {}
        for field, col_ref in node._input_column_bindings.items():
            if col_ref.node in results:
                upstream_dfs[col_ref.node] = results[col_ref.node]

        # Use the finest-grained index from upstream
        all_indices = [set(df.index) for df in upstream_dfs.values()]

        def _max_depth(idx_set: set) -> int:
            return max((str(i).count('::') for i in idx_set), default=0)

        finest_index = sorted(
            max(all_indices, key=lambda s: (_max_depth(s), len(s))),
            key=str,
        )

        # Build proxy DataFrame: only column-bound fields
        rows = []
        for idx in finest_index:
            row: dict[str, Any] = {}
            for field, col_ref in node._input_column_bindings.items():
                up_df = results[col_ref.node]
                idx_set = set(str(i) for i in up_df.index)
                if idx in idx_set:
                    row[field] = up_df.at[idx, col_ref.column]
                else:
                    parent = self._find_parent_index(idx, idx_set)
                    if parent is not None:
                        row[field] = up_df.at[parent, col_ref.column]
            rows.append(row)

        return pd.DataFrame(rows, index=pd.Index([str(i) for i in finest_index]))

    def _assemble_sub_workflow_output(
        self,
        node: "SubWorkflowNode",
        results: dict[Node, pd.DataFrame],
    ) -> pd.DataFrame:
        """Assemble the sub-workflow output from its output mapping."""
        # Collect columns from internal nodes
        output_data: dict[str, pd.Series] = {}
        reference_index = None

        for field, col_ref in node._output_mapping.items():
            if col_ref.node not in results:
                raise RuntimeError(
                    f"Internal node '{col_ref.node.name}' not executed — "
                    f"cannot assemble SubWorkflow output."
                )
            df = results[col_ref.node]
            output_data[field] = cast(pd.Series, df[col_ref.column])
            if reference_index is None:
                reference_index = df.index

        if not output_data:
            return pd.DataFrame()

        output_df = pd.DataFrame(output_data, index=reference_index)
        output_df.index = output_df.index.astype(str)
        return output_df

    # ── Cache persistence ──────────────────────────────────────────────

    def _save_and_cleanup(
        self,
        node_dir: Path,
        sig_hash: str,
        df: pd.DataFrame,
        tool_name: str,
        workflow: Any,
        parameters: dict[str, Any] | None = None,
        hash_dir: Path | None = None,
    ) -> None:
        """Save results to cache and run cleanup."""
        cache_save(node_dir, sig_hash, df, metadata={
            "tool": tool_name,
            "timestamp": time.time(),
        }, parameters=parameters, hash_dir=hash_dir)
        cleanup_cache(node_dir, workflow.max_executions, workflow.max_age)

    # ── Index alignment ────────────────────────────────────────────────

    def _align_dataframes_for_merge(self, dfs: list[pd.DataFrame]) -> list[pd.DataFrame]:
        """Align DataFrames with different index granularity for merge.

        Uses ``::`` depth to determine the finest-grained index rather than
        row count, which is correct when some DataFrames have fewer rows due
        to filtering rather than coarser granularity.
        """
        if len(dfs) <= 1:
            return dfs

        def _max_depth(index: pd.Index) -> int:
            return max((str(i).count('::') for i in index), default=0)

        finest_idx = max(range(len(dfs)), key=lambda i: (_max_depth(dfs[i].index), len(dfs[i])))
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
                expanded_df = pd.DataFrame(expanded_rows, index=pd.Index(expanded_indices))
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
            return max((str(i).count('::') for i in idx_set), default=0)

        all_indices = [set(results[n].index) for n in upstream_nodes.values()]
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
                        safe_idx = str(idx).replace('::', '_')
                        npy_path = assets_dir / f"shm_{col}_{safe_idx}.npy"
                        with open_shared_array(val) as arr:
                            np.save(str(npy_path), arr)
                    except (OSError, ValueError) as e:
                        logger.warning(
                            "Failed to persist SharedArray col=%s idx=%s: %s", col, idx, e
                        )
        return df

    def _restore_shared_arrays(self, df: pd.DataFrame, hash_dir: Path) -> pd.DataFrame:
        """Restore SharedArray columns from .npy files on cache load."""
        assets_dir = get_assets_dir(hash_dir)
        for col in df.columns:
            for idx in df.index:
                val = df.at[idx, col]
                if isinstance(val, str):
                    safe_idx = str(idx).replace('::', '_')
                    npy_path = assets_dir / f"shm_{col}_{safe_idx}.npy"
                    if npy_path.exists():
                        try:
                            from bioimageflow_core.shm import create_shared_output
                            data = np.load(str(npy_path))
                            with create_shared_output(data) as ref:
                                df.at[idx, col] = ref  # type: ignore[call-overload]
                        except (OSError, ValueError) as e:
                            logger.warning(
                                "Failed to restore SharedArray col=%s idx=%s: %s",
                                col, idx, e,
                            )
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
        message: str | None = None,
        current: int | None = None,
        maximum: int | None = None,
    ) -> None:
        """Emit a progress event, serialized via ``_progress_lock``."""
        if workflow.on_progress is not None:
            from bioimageflow.workflow import ProgressEvent
            event = ProgressEvent(
                node_name=node_name,
                status=status,
                row=row,
                total_rows=total_rows,
                message=message,
                current=current,
                maximum=maximum,
                timestamp=time.time(),
            )
            with self._progress_lock:
                workflow.on_progress(event)


class SequentialEngine(DefaultEngine):
    """Forces sequential execution — useful for debugging and deterministic reproduction.

    Inherits from :class:`DefaultEngine` but forces ``_force_sequential=True``
    and overrides worker resolution to always use a single worker with no
    ``worker_env``.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs["force_sequential"] = True
        super().__init__(**kwargs)

    def _resolve_worker_config(self, tool: ProcessingTool, workflow: Any) -> tuple[int, Any]:
        """Always single-worker, no worker_env — truly sequential."""
        return 1, None
