"""Public engine façade and shared workflow scheduler."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import (
    Any,
    DirectBackend,
    DisabledNodeError,
    EnvironmentLifetime,
    Generator,
    Node,
    NodeStep,
    ProcessingBackend,
    ProcessingTool,
    TopologicalSorter,
    WetlandsBackend,
    WorkflowCancelledError,
    concurrent,
    logger,
    pd,
    scoped_node_names,
    threading,
    time,
)
from bioimageflow.events import ProgressEvent
from .graph import _GraphMixin
from .cache_runtime import _CacheRuntimeMixin
from .node_execution import _NodeExecutionMixin
from .arguments import _ArgumentsMixin
from .dispatch import _DispatchMixin
from .dataframes import _DataframesMixin
from .planning import _PlanningMixin

if TYPE_CHECKING:
    from bioimageflow.env_manager import WetlandsEnvManager


class DefaultEngine(
    _GraphMixin,
    _CacheRuntimeMixin,
    _NodeExecutionMixin,
    _ArgumentsMixin,
    _DispatchMixin,
    _DataframesMixin,
    _PlanningMixin,
):
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
        env_manager: "WetlandsEnvManager | None" = None,
        environment_lifetime: EnvironmentLifetime | str = EnvironmentLifetime.EXECUTION,
    ) -> None:
        try:
            self._environment_lifetime = EnvironmentLifetime(environment_lifetime)
        except ValueError as exc:
            expected = ", ".join(lifetime.value for lifetime in EnvironmentLifetime)
            raise ValueError(
                f"Unknown environment_lifetime '{environment_lifetime}'. Expected {expected}."
            ) from exc
        if env_manager is not None and not use_wetlands:
            raise ValueError("env_manager requires use_wetlands=True.")
        if (
            self._environment_lifetime is EnvironmentLifetime.EXTERNAL
            and env_manager is None
        ):
            raise ValueError(
                "environment_lifetime='external' requires an injected env_manager."
            )

        self._use_wetlands = use_wetlands
        self._force_sequential = force_sequential
        self._progress_lock = threading.Lock()
        self._cache_hit_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._closed = False
        self._node_cache_hits: dict[Node, bool] = {}
        self._env_manager = env_manager
        if use_wetlands:
            if self._env_manager is None:
                from bioimageflow.env_manager import WetlandsEnvManager

                self._env_manager = WetlandsEnvManager(**(wetlands_config or {}))
            self._backend: ProcessingBackend = WetlandsBackend()
        else:
            self._backend = DirectBackend()

    @property
    def environment_lifetime(self) -> EnvironmentLifetime:
        """Return the engine's Wetlands environment ownership policy."""
        return self._environment_lifetime

    @property
    def environment_manager(self) -> "WetlandsEnvManager | None":
        """Return the Wetlands manager used by this engine, if any."""
        return self._env_manager

    def _ensure_open(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("This execution engine is closed.")

    @staticmethod
    def _column_label(col_ref: Any) -> str:
        """Resolve stable workflow output IDs to boundary DataFrame labels."""
        from bioimageflow.workflow_node import WorkflowNode

        if isinstance(col_ref.node, WorkflowNode):
            return col_ref.node.output_name_for_id(col_ref.column)
        return col_ref.column

    def _cleanup_after_execution(self) -> None:
        self._backend.cleanup_execution(self)

    def close(self) -> None:
        """Close the engine and stop environments it owns.

        The method is idempotent. Externally owned managers are never shut
        down; their owner must call :meth:`WetlandsEnvManager.shutdown_all`.
        """
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._backend.close(self)

    def __enter__(self) -> "DefaultEngine":
        """Return an open engine for context-manager use."""
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        """Close the engine when leaving a context manager."""
        self.close()

    def execute(self, targets: list[Node], workflow: Any) -> dict[str, pd.DataFrame]:
        """Execute the workflow, returning results for target nodes."""
        self._ensure_open()
        try:
            return self._execute_impl(targets, workflow)
        finally:
            self._cleanup_after_execution()

    def execute_steps(
        self,
        targets: list[Node],
        workflow: Any,
    ) -> Generator[NodeStep, None, None]:
        """Yield a :class:`NodeStep` for each node in topological order.

        The engine (and any Wetlands environments) stays alive between yields.
        The caller controls execution via ``step.prepare()`` / ``step.execute()``.
        If ``execute()`` is not called before advancing, the step auto-executes
        to keep the results chain consistent for downstream nodes.

        WorkflowNodes are expanded: their internal nodes are yielded
        individually with scoped names (``workflow_node/internal_name``).

        Cleanup runs when the generator is exhausted or closed (early ``break``).
        """
        self._ensure_open()
        try:
            reachable, completion_dependencies, scoped_names = (
                self._compile_execution_graph(targets)
            )

            self._check_env_mismatches(reachable)
            with scoped_node_names(scoped_names):
                order = self._topological_sort(
                    reachable,
                    completion_dependencies,
                )
                _executable, skipped = self._filter_executable(
                    order,
                    completion_dependencies,
                )

                results: dict[Node, pd.DataFrame] = {}
                sig_hashes: dict[Node, str] = {}

                for node in order:
                    if workflow.cancel_requested:
                        raise WorkflowCancelledError("Workflow cancelled by user")

                    if node in skipped:
                        logger.info(
                            "Skipping node '%s' (disabled or upstream disabled)",
                            node.name,
                        )
                        from bioimageflow.workflow_node import WorkflowNode

                        if not isinstance(node, WorkflowNode):
                            yield NodeStep(
                                node,
                                self,
                                results,
                                sig_hashes,
                                workflow,
                                skipped=True,
                            )
                        continue

                    from bioimageflow.workflow_node import WorkflowNode

                    if isinstance(node, WorkflowNode):
                        df, sig_hash = self._execute_workflow_node(
                            node,
                            results,
                            sig_hashes,
                            workflow,
                        )
                        results[node] = df
                        sig_hashes[node] = sig_hash
                        continue

                    step = NodeStep(node, self, results, sig_hashes, workflow)
                    yield step
                    if not step._executed:
                        step.execute()
                    results[node] = step._df  # type: ignore[assignment]
                    sig_hashes[node] = step._sig_hash  # type: ignore[assignment]
        finally:
            self._cleanup_after_execution()

    def _execute_impl(
        self, targets: list[Node], workflow: Any
    ) -> dict[str, pd.DataFrame]:
        """Internal execution logic.

        Uses ``TopologicalSorter.get_ready()`` / ``.done()`` to find
        independent nodes and, when ``_force_sequential`` is ``False``,
        dispatches independent ``ProcessingTool`` nodes concurrently via
        ``ThreadPoolExecutor``.  ``DataFrameTool`` nodes always run on
        the main thread.
        """
        reachable, completion_dependencies, scoped_names = (
            self._compile_execution_graph(targets)
        )
        self._check_env_mismatches(reachable)
        with scoped_node_names(scoped_names):
            return self._execute_compiled_impl(
                targets,
                workflow,
                reachable,
                completion_dependencies,
            )

    def _execute_compiled_impl(
        self,
        targets: list[Node],
        workflow: Any,
        reachable: set[Node],
        completion_dependencies: dict[Node, set[Node]],
    ) -> dict[str, pd.DataFrame]:
        """Execute one flattened recursive graph."""
        from bioimageflow.dataframe_tool import DataFrameTool

        order = self._topological_sort(reachable, completion_dependencies)
        executable, skipped = self._filter_executable(
            order,
            completion_dependencies,
        )

        # Check that at least one target is executable
        executable_targets = [t for t in targets if t not in skipped]
        if not executable_targets:
            disabled_names = [t.name for t in targets]
            raise DisabledNodeError(
                f"All target nodes are disabled or have disabled "
                f"upstream dependencies: {disabled_names}"
            )

        dep_graph = self._build_dep_graph_from_set(
            set(executable),
            completion_dependencies,
        )
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
                cache_hit = self._pop_node_cache_hit(node)
                self._write_run_node_view(workflow, node, sig_hash, cache_hit=cache_hit)
                results[node] = df
                sig_hashes[node] = sig_hash
                ts.done(node)

            if len(pt_nodes) <= 1 or self._force_sequential:
                # Single node or forced sequential — no threading overhead
                for node in pt_nodes:
                    if workflow.cancel_requested:
                        raise WorkflowCancelledError("Workflow cancelled by user")
                    df, sig_hash = self._execute_node(
                        node, results, sig_hashes, workflow
                    )
                    cache_hit = self._pop_node_cache_hit(node)
                    self._write_run_node_view(
                        workflow, node, sig_hash, cache_hit=cache_hit
                    )
                    results[node] = df
                    sig_hashes[node] = sig_hash
                    ts.done(node)
            else:
                # Multiple independent ProcessingTool nodes — run concurrently.
                # Cap threads to avoid excessive overhead; each thread mostly
                # waits on Wetlands IPC, so the cap is generous.
                pool_size = min(len(pt_nodes), 8)
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=pool_size
                ) as pool:
                    future_to_node = {
                        pool.submit(
                            self._execute_node, node, results, sig_hashes, workflow
                        ): node
                        for node in pt_nodes
                    }
                    first_error: Exception | None = None
                    for future in concurrent.futures.as_completed(future_to_node):
                        node = future_to_node[future]
                        try:
                            df, sig_hash = future.result()
                        except Exception as exc:
                            if first_error is None:
                                first_error = exc
                            continue
                        cache_hit = self._pop_node_cache_hit(node)
                        self._write_run_node_view(
                            workflow, node, sig_hash, cache_hit=cache_hit
                        )
                        with lock:
                            results[node] = df
                            sig_hashes[node] = sig_hash
                        ts.done(node)
                    if first_error is not None:
                        raise first_error

        # Log skipped targets
        for t in targets:
            if t in skipped:
                logger.info("Skipping disabled target node '%s'", t.name)

        return {t.name: results[t] for t in targets if t not in skipped}

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
        result_key: str | None = None,
        record_id: str | None = None,
    ) -> None:
        """Emit a progress event, serialized via ``_progress_lock``."""
        if workflow.on_progress is not None:
            event = ProgressEvent(
                node_name=node_name,
                status=status,
                result_key=result_key,
                record_id=record_id,
                row=row,
                total_rows=total_rows,
                message=message,
                current=current,
                maximum=maximum,
                timestamp=time.time(),
            )
            with self._progress_lock:
                workflow.on_progress(event)

    # ── Pre-execution planning ─────────────────────────────────────────


class SequentialEngine(DefaultEngine):
    """Forces sequential execution — useful for debugging and deterministic reproduction.

    Inherits from :class:`DefaultEngine` but forces ``_force_sequential=True``
    and overrides worker resolution to always use a single worker with no
    ``worker_env``.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs["force_sequential"] = True
        super().__init__(**kwargs)

    def _resolve_worker_config(
        self,
        tool: ProcessingTool,
        workflow: Any,
    ) -> tuple[int, Any, float | None]:
        """Always single-worker, no worker_env — truly sequential.

        ``worker_timeout`` is still honored from ``get_environment()`` so a
        hung tool doesn't block the sequential engine indefinitely.
        """
        env_config = workflow._env_configs.get(tool.environment.name)
        worker_timeout = env_config.worker_timeout if env_config else None
        return 1, None, worker_timeout
