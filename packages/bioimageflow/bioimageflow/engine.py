"""Execution engines for BioImageFlow workflows."""

import concurrent.futures
import hashlib
import inspect
import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import UnionType
from collections.abc import Generator
from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Any, cast, get_args, get_origin, TYPE_CHECKING, Union

import pandas as pd

from bioimageflow_core.arguments import Arguments, ExecutionContext
from bioimageflow_core.tool import ProcessingTool, Template
from bioimageflow_core.types import SharedArray
from bioimageflow_core.environment import EnvironmentMismatchError
from bioimageflow.cache import (
    compute_env_hash,
    compute_signature_hash,
    dataframe_lookup,
    dataframe_publish,
    dataframe_result_key,
    iter_dataframe_result_metadata,
    iter_processing_result_metadata,
    processing_lookup,
    processing_prepare_attempt,
    processing_publish,
    processing_result_key,
)
from bioimageflow.node import IndexAlignmentError, Node
from bioimageflow.storage import CacheCorruptionError, Storage, validate_relative_posix_path
from bioimageflow.template import get_output_templates, resolve_template
from bioimageflow.validation import get_tool_version, get_source_hash, is_path_type

if TYPE_CHECKING:
    from bioimageflow.env_manager import WetlandsEnvManager
    from bioimageflow.sub_workflow import SubWorkflowNode

logger = logging.getLogger("bioimageflow")


class EnvironmentLifetime(str, Enum):
    """Ownership policy for Wetlands environments used by an engine.

    ``EXECUTION`` preserves the historical behavior and stops environments
    after every :meth:`DefaultEngine.execute` or
    :meth:`DefaultEngine.execute_steps` call. ``ENGINE`` retains them until
    :meth:`DefaultEngine.close`. ``EXTERNAL`` leaves cleanup entirely to the
    owner of the injected environment manager.
    """

    EXECUTION = "execution"
    ENGINE = "engine"
    EXTERNAL = "external"


def _accepts_context(method: Any) -> bool:
    """Return True when a ProcessingTool method declares a context kwarg."""
    return "context" in inspect.signature(method).parameters


def _safe_work_dir_name(position: int, row_index: Any) -> str:
    """Build a collision-resistant directory name for one input row."""
    raw = str(row_index)
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._-")
    if not sanitized:
        sanitized = "row"
    sanitized = sanitized[:48]
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{position:06d}_{sanitized}_{digest}"


def _assets_dir(run_dir: Path) -> Path:
    return run_dir / "assets"


def _work_dir(run_dir: Path) -> Path:
    return run_dir / "work"


def _rows_work_dir(run_dir: Path) -> Path:
    return _work_dir(run_dir) / "rows"


def _pending_assets_dir(storage_path: str | Path, node_name: str) -> Path:
    return Path(storage_path) / "cache" / "v1" / "pending" / node_name / "assets"


def _batch_work_dir(run_dir: Path) -> Path:
    return _work_dir(run_dir) / "batch"


def _absolute_runtime_path(value: Any) -> Any:
    """Return ``value`` as an absolute path when it is path-like."""
    if value is None or isinstance(value, SharedArray):
        return value
    if isinstance(value, str) and value == "":
        return value
    preserve_path = isinstance(value, Path)
    try:
        path = Path(value).expanduser()
    except TypeError:
        return value
    if not path.is_absolute():
        path = Path.cwd() / path
    return path if preserve_path else str(path)


def topological_order(workflow: Any) -> list[str]:
    """Return the names of the nodes in ``workflow`` in dependency order.

    Uses ``graphlib.TopologicalSorter`` over all registered nodes. Raises
    ``CycleError`` if the graph contains a cycle; callers that want to
    tolerate cycles should catch that or use :meth:`Workflow.validate`.
    """
    dep_graph: dict[str, set[str]] = {}
    for name, node in workflow._nodes.items():
        deps: set[str] = set()
        for up in node._upstream_nodes:
            if up.name in workflow._nodes:
                deps.add(up.name)
        for arg in node._args:
            if isinstance(arg, Node) and arg.name in workflow._nodes:
                deps.add(arg.name)
        dep_graph[name] = deps
    return list(TopologicalSorter(dep_graph).static_order())


def source_processing_signature_material(node: Node) -> dict[str, Any]:
    """Return the cache-signature material for source ProcessingTool nodes."""
    assert isinstance(node.tool, ProcessingTool)
    input_annotations = node.tool.Inputs._get_all_annotations()
    assert node.tool.Outputs is not None
    templates = get_output_templates(
        node.tool.Outputs,
        node.tool.Inputs,
        node.output_templates,
    )
    signature_constants = dict(node._constant_bindings)
    for field_name, annotation in input_annotations.items():
        if field_name not in signature_constants and hasattr(node.tool.Inputs, field_name):
            signature_constants[field_name] = getattr(node.tool.Inputs, field_name)
        if field_name in signature_constants and is_path_type(annotation):
            signature_constants[field_name] = _absolute_runtime_path(signature_constants[field_name])
    return {
        "constants": signature_constants,
        "output_templates": templates,
    }


def _resolve_staged_output_path(assets_dir: Path, template: str, context: dict[str, Any]) -> str:
    resolved = resolve_template(template, context)
    try:
        relative = validate_relative_posix_path(Path(resolved).as_posix())
    except ValueError as exc:
        raise CacheCorruptionError(f"Unsafe output template path: {resolved!r}") from exc
    path = assets_dir / relative
    try:
        path.resolve().relative_to(assets_dir.resolve())
    except ValueError as exc:
        raise CacheCorruptionError(f"Output template path escapes staging assets: {resolved!r}") from exc
    return str(path)


def _to_python(val: Any) -> Any:
    """Convert numpy scalars to native Python types.

    pandas DataFrames store numeric values as numpy scalars (np.int64,
    np.float64, etc.).  When these are pickled and sent to Wetlands worker
    environments that don't have numpy installed, unpickling fails.
    The ``.item()`` method is the standard way to get the native Python
    equivalent and is available on all numpy scalar types.
    """
    return val.item() if hasattr(val, "item") else val


def _compute_engine_timeout(worker_timeout: float | None) -> float | None:
    """Compute the engine-side safety timeout from the Wetlands worker timeout.

    The multiplier ensures the Wetlands health monitor fires first. The
    engine-side timeout is a last resort when the health monitor fails to
    catch a dead or hung worker (e.g., replacement worker itself is stuck).
    """
    if worker_timeout is None:
        return None
    return max(worker_timeout * 1.5, worker_timeout + 60.0)


def _path_output_columns(tool: Any) -> set[str]:
    outputs = getattr(tool, "Outputs", None)
    if outputs is None or not hasattr(outputs, "_get_all_annotations"):
        return set()
    return {
        field_name
        for field_name, annotation in outputs._get_all_annotations().items()
        if is_path_type(annotation)
    }


def _explicit_template_output_columns(node: Node) -> set[str]:
    outputs = getattr(node.tool, "Outputs", None)
    if outputs is None or not hasattr(outputs, "_get_all_annotations"):
        return set()
    columns = {
        field_name
        for field_name in outputs._get_all_annotations()
        if isinstance(getattr(outputs, field_name, None), Template)
    }
    columns.update(
        field_name
        for field_name, template in node.output_templates.items()
        if isinstance(template, str) and template
    )
    return columns


def _declared_owned_artifact_paths(
    arguments_dicts: list[dict[str, Any]],
    execution_index: list[Any],
    df: pd.DataFrame,
    output_columns: set[str],
) -> list[tuple[str, Any, Any]]:
    artifacts: list[tuple[str, Any, Any]] = []
    output_indices = {str(index) for index in df.index}
    for row_index, row_args in zip(execution_index, arguments_dicts):
        row_index_str = str(row_index)
        if row_index_str in output_indices or any(
            index.startswith(f"{row_index_str}::") for index in output_indices
        ):
            continue
        for column in sorted(output_columns):
            if column in row_args:
                artifacts.append((column, row_index, row_args[column]))
    return artifacts


def _declared_zero_row_scalar_outputs(
    tool: ProcessingTool,
    raw_results: list[list[Any]],
    execution_index: list[Any],
) -> list[tuple[str, Any, Any]]:
    declared_values = getattr(tool, "zero_row_scalar_outputs", {})
    if not declared_values:
        return []
    if not isinstance(declared_values, dict):
        raise TypeError(f"{type(tool).__name__}.zero_row_scalar_outputs must be a dict.")
    outputs = getattr(tool, "Outputs", None)
    if outputs is None or not hasattr(outputs, "_get_all_annotations"):
        return []
    output_annotations = outputs._get_all_annotations()
    entries: list[tuple[str, Any, Any]] = []
    for column, value in sorted(declared_values.items()):
        column_name = str(column)
        if column_name not in output_annotations:
            raise ValueError(
                f"{type(tool).__name__}.zero_row_scalar_outputs declares unknown output column {column_name!r}."
            )
        annotation = output_annotations[column_name]
        if is_path_type(annotation) or _is_shared_array_type(annotation):
            raise ValueError(
                f"{type(tool).__name__}.zero_row_scalar_outputs column {column_name!r} must be scalar."
            )
        for row_index, row_outputs in zip(execution_index, raw_results):
            if len(row_outputs) == 0:
                entries.append((column_name, row_index, value))
    return entries


def _is_shared_array_type(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _is_shared_array_type(get_args(annotation)[0])
    if origin is Union or origin is UnionType:
        return any(_is_shared_array_type(arg) for arg in get_args(annotation) if arg is not type(None))
    return annotation is SharedArray


def _has_shared_array_output(tool: Any) -> bool:
    outputs = getattr(tool, "Outputs", None)
    if outputs is None or not hasattr(outputs, "_get_all_annotations"):
        return False
    return any(_is_shared_array_type(annotation) for annotation in outputs._get_all_annotations().values())


def _shared_array_output_columns(tool: Any) -> set[str]:
    outputs = getattr(tool, "Outputs", None)
    if outputs is None or not hasattr(outputs, "_get_all_annotations"):
        return set()
    return {
        name
        for name, annotation in outputs._get_all_annotations().items()
        if _is_shared_array_type(annotation)
    }


def _processing_has_other_current(storage_path: str | Path, node_name: str, sig_hash: str) -> bool:
    expected_key = processing_result_key(node_name, sig_hash)
    for metadata in iter_processing_result_metadata(
        storage_path,
        {node_name: {sig_hash}},
    ):
        if (
            metadata.get("node") != node_name
            or metadata.get("result_key") == expected_key
        ):
            continue
        return True
    return False


def _dataframe_has_other_current(storage_path: str | Path, node_name: str, sig_hash: str) -> bool:
    expected_key = dataframe_result_key(node_name, sig_hash)
    for metadata in iter_dataframe_result_metadata(
        storage_path,
        {node_name: {sig_hash}},
    ):
        if (
            metadata.get("node") != node_name
            or metadata.get("result_key") == expected_key
        ):
            continue
        return True
    return False


class DisabledNodeError(Exception):
    """Raised when all requested target nodes are disabled or unreachable."""
    pass


class WorkflowCancelledError(Exception):
    """Raised when a workflow execution is cancelled via ``Workflow.cancel()``."""
    pass


class CycleInWorkflowError(ValueError):
    """Raised by :meth:`DefaultEngine.plan` / :meth:`Workflow.plan` when the
    graph contains a cycle.

    Use :meth:`Workflow.validate` for non-fatal cycle reporting (it returns
    a :class:`ValidationError` with ``kind="cycle"`` instead of raising).
    """

    def __init__(self, nodes: list[str]) -> None:
        self.nodes = list(nodes)
        super().__init__(f"Cycle detected in workflow graph: {self.nodes}")


class NodePlanStatus(str, Enum):
    """Per-node status returned alongside :class:`NodePlan`.

    Use this enum (or its string values) instead of inspecting the cache
    directory layout — the platform should not need to know how the
    library stores cached node results.

    Values
    ------
    CACHED
        The node result key has a valid selected current record;
        ``compute()`` would short-circuit.
    PRIOR_SELECTION_MISS
        The planned result key has no selected current record, but the
        same node has another selected current record from prior result-key
        material. ``compute()`` would re-execute and publish or select a record.
    UNEXECUTED
        No known cache record exists for this node.
    SKIPPED
        The node is disabled, or has a disabled upstream that prevents
        execution. ``final_result_key`` and ``selected_record_id`` are ``None``.
    PENDING_UPSTREAM
        At least one upstream selected record is not known yet, so the
        node's final result key cannot be determined from a stable
        record graph snapshot.
    """
    CACHED = "cached"
    PRIOR_SELECTION_MISS = "prior_selection_miss"
    UNEXECUTED = "unexecuted"
    SKIPPED = "skipped"
    PENDING_UPSTREAM = "pending_upstream"


@dataclass(frozen=True)
class NodePlan:
    """A single node's pre-execution plan entry, returned by :meth:`DefaultEngine.plan`.

    Attributes
    ----------
    node_name
        Scoped node name (``"subworkflow/internal"`` for sub-workflow
        internals, plain name otherwise).
    final_result_key
        V1 result key for this node when it can be computed from known
        selected upstream records; ``None`` for skipped and pending nodes.
    selected_record_id
        Currently selected record ID for ``final_result_key`` when the
        node is cached; otherwise ``None``.
    status
        Per-node :class:`NodePlanStatus` — the canonical signal for
        external callers wanting to display "cached / prior selection miss /
        unexecuted / skipped" in a GUI.
    upstream
        Scoped names of this node's direct upstreams.
    pending_upstreams
        Upstream node names whose selected records are not known yet.
    logical_signature
        Diagnostic logical signature computed by the same helpers as
        execution. It is not the public cache identity field.

    ``cached`` and ``skipped`` are read-only convenience accessors
    derived from ``status`` (``cached == status is CACHED``,
    ``skipped == status is SKIPPED``).
    """
    node_name: str
    logical_signature: str
    status: NodePlanStatus
    upstream: tuple[str, ...]
    final_result_key: str | None = None
    selected_record_id: str | None = None
    pending_upstreams: tuple[str, ...] = ()

    @property
    def cached(self) -> bool:
        return self.status is NodePlanStatus.CACHED

    @property
    def skipped(self) -> bool:
        return self.status is NodePlanStatus.SKIPPED


class WorkerTimeoutError(RuntimeError):
    """Raised when the engine-side safety timeout fires on a Wetlands task.

    This is a last-resort timeout that wraps ``task.wait_for()`` in
    ``_dispatch_via_wetlands``.  It only fires when the Wetlands-side
    health monitor fails to catch a dead or hung worker within
    ``worker_timeout * 1.5`` (or ``worker_timeout + 60``, whichever is
    larger).
    """
    pass


class WorkerTaskError(RuntimeError):
    """Raised when a Wetlands worker task fails while executing a node."""

    def __init__(
        self,
        message: str | None = None,
        *,
        node_name: str = "",
        tool_class: str = "",
        environment_name: str = "",
        row_index: str | None = None,
        original: BaseException | None = None,
        task_status: Any = None,
        task_traceback: Any = None,
    ) -> None:
        self.node_name = node_name
        self.tool_class = tool_class
        self.environment_name = environment_name
        self.row_index = row_index
        self.original = original
        self.task_status = task_status
        self.task_traceback = task_traceback

        if message is None:
            scope = "batch task" if row_index is None else f"row {row_index}"
            lines = [
                f"Worker task failed for node '{node_name}' ({scope}).",
                f"Tool: {tool_class}",
                f"Environment: {environment_name}",
            ]
            if task_status is not None:
                lines.append(f"Task status: {task_status}")
            if original is not None:
                lines.append(f"Original error: {type(original).__name__}: {original}")
            if task_traceback:
                if isinstance(task_traceback, str):
                    traceback_text = task_traceback
                else:
                    traceback_text = "\n".join(str(line) for line in task_traceback)
                lines.append(f"Remote traceback:\n{traceback_text}")
            message = "\n".join(lines)

        super().__init__(message)


def _raise_worker_task_error(
    task: Any,
    *,
    node_name: str,
    tool: ProcessingTool,
    row_index: str | None,
) -> None:
    original = getattr(task, "exception", None)
    if original is not None and not isinstance(original, BaseException):
        original = RuntimeError(str(original))
    error = WorkerTaskError(
        node_name=node_name,
        tool_class=type(tool).__name__,
        environment_name=tool.environment.name,
        row_index=row_index,
        original=original,
        task_status=getattr(task, "status", None),
        task_traceback=getattr(task, "traceback", None),
    )
    if original is None:
        raise error
    raise error from original


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

        The first access triggers logical-signature computation and cache
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
            max_workers, worker_env, worker_timeout = self._engine._resolve_worker_config(
                self._node.tool, self._workflow,
            )
            self._engine._env_manager.get_or_create(
                self._node.tool.environment,
                max_workers=max_workers,
                worker_env=worker_env,
                worker_timeout=worker_timeout,
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
            assert self._sig_hash is not None
            result_key = self._engine._node_result_key(self._node, self._sig_hash)
            self._engine._write_run_node_view(
                self._workflow,
                self._node,
                self._sig_hash,
                cache_hit=True,
            )
            if result_key is not None:
                self._engine._emit_progress(
                    self._workflow,
                    self._node.name,
                    "cached",
                    result_key=result_key,
                    record_id=self._engine._selected_record_id(self._workflow, result_key),
                )
            self._executed = True
            return self._df
        df, sig_hash = self._engine._execute_node(
            self._node, self._results, self._sig_hashes, self._workflow,
        )
        self._df = df
        self._sig_hash = sig_hash
        cache_hit = self._engine._pop_node_cache_hit(self._node)
        self._engine._write_run_node_view(
            self._workflow,
            self._node,
            sig_hash,
            cache_hit=cache_hit,
        )
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
        if self._environment_lifetime is EnvironmentLifetime.EXTERNAL and env_manager is None:
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

    def _cleanup_after_execution(self) -> None:
        if (
            self._environment_lifetime is EnvironmentLifetime.EXECUTION
            and self._env_manager is not None
        ):
            self._env_manager.shutdown_all()

    def close(self) -> None:
        """Close the engine and stop environments it owns.

        The method is idempotent. Externally owned managers are never shut
        down; their owner must call :meth:`WetlandsEnvManager.shutdown_all`.
        """
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            if (
                self._env_manager is not None
                and self._environment_lifetime is not EnvironmentLifetime.EXTERNAL
            ):
                self._env_manager.shutdown_all()

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
        self._ensure_open()
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
            self._cleanup_after_execution()

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

                if workflow.cancel_requested:
                    raise WorkflowCancelledError("Workflow cancelled by user")

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
                    df, sig_hash = self._execute_node(node, results, sig_hashes, workflow)
                    cache_hit = self._pop_node_cache_hit(node)
                    self._write_run_node_view(workflow, node, sig_hash, cache_hit=cache_hit)
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
                        self._write_run_node_view(workflow, node, sig_hash, cache_hit=cache_hit)
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
        *,
        hydrate_assets: bool = True,
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
            upstream_identities = self._upstream_identity_map(
                workflow,
                [arg for arg in node._args if isinstance(arg, Node) and arg in sig_hashes],
                sig_hashes,
            )
            sig_hash = self._compute_sig_hash(node, "", args_dict, upstream_identities, workflow)
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
                    cr.node.name: cr.node
                    for cr in node._column_bindings.values()
                }
                sig_hash = self._compute_processing_sig_hash(
                    node, input_annotations, upstream_nodes, sig_hashes, workflow,
                )
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

    def _execute_node(
        self,
        node: Node,
        results: dict[Node, pd.DataFrame],
        sig_hashes: dict[Node, str],
        workflow: Any,
    ) -> tuple[pd.DataFrame, str]:
        """Execute a single node, returning its DataFrame and logical digest."""
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

        upstream_identities = self._upstream_identity_map(
            workflow,
            [arg for arg in node._args if isinstance(arg, Node) and arg in sig_hashes],
            sig_hashes,
        )
        sig_hash = self._compute_sig_hash(node, "", args_dict, upstream_identities, workflow)

        result_key = dataframe_result_key(node.name, sig_hash)
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

        df = self._coerce_numeric_columns(
            dataframe_publish(workflow.storage_path, node.name, sig_hash, df)
        )
        self._emit_progress(
            workflow,
            node.name,
            "completed",
            result_key=result_key,
            record_id=self._selected_record_id(workflow, result_key),
        )
        df = self._normalize_path_output_columns(df, node.tool)
        self._set_node_cache_hit(node, False)
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
        result_key, attempt_id, staging_dir, real_assets_dir = processing_prepare_attempt(
            workflow.storage_path,
            node.name,
            sig_hash,
        )

        row_args = self._resolve_defaults(node, input_annotations)
        path_input_fields = [
            n for n, a in input_annotations.items() if is_path_type(a)
        ]
        context = self._build_template_context(
            node.name, '0', row_args,
            path_input_fields=path_input_fields, upstream_nodes={},
            results={}, idx='0',
        )
        for out_field, template in templates.items():
            row_args[out_field] = _resolve_staged_output_path(real_assets_dir, template, context)
        arguments_dicts = [row_args]

        # --- Dispatch & build output ---
        row_contexts, batch_context = self._build_execution_contexts(
            staging_dir, real_assets_dir, aligned_index,
        )
        raw_results = self._dispatch_tool(
            node.tool, arguments_dicts, workflow, node.name,
            row_contexts, batch_context,
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
        sig_hashes: dict[Node, str],
        workflow: Any,
    ) -> tuple[pd.DataFrame, str]:
        """Execute a ProcessingTool node that has upstream column bindings."""
        assert isinstance(node.tool, ProcessingTool)

        input_annotations = node.tool.Inputs._get_all_annotations()
        assert node.tool.Outputs is not None  # ProcessingTool always has Outputs
        templates = get_output_templates(
            node.tool.Outputs,
            node.tool.Inputs,
            node.output_templates,
        )

        upstream_nodes = {cr.node.name: cr.node
                         for cr in node._column_bindings.values()}
        aligned_index, _ = self._align_indices(node, upstream_nodes, results)
        self._validate_column_bindings(node, results)

        # --- Signature hash ---
        sig_hash = self._compute_processing_sig_hash(
            node, input_annotations, upstream_nodes, sig_hashes, workflow,
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
        result_key, attempt_id, staging_dir, real_assets_dir = processing_prepare_attempt(
            workflow.storage_path,
            node.name,
            sig_hash,
        )

        path_input_fields = [n for n, a in input_annotations.items() if is_path_type(a)]
        execution_index = aligned_index
        has_batch = type(node.tool).process_batch is not ProcessingTool.process_batch
        if not aligned_index and has_batch and getattr(node.tool, "run_empty_batch", False):
            execution_index, arguments_dicts = self._resolve_empty_batch_arguments(
                node,
                results,
                input_annotations,
                templates,
                path_input_fields,
                real_assets_dir,
            )
        else:
            arguments_dicts = self._resolve_all_row_arguments(
                node, aligned_index, results, upstream_nodes,
                input_annotations, templates, path_input_fields, workflow,
                assets_dir=real_assets_dir,
            )

        # --- Dispatch & build output ---
        row_contexts, batch_context = self._build_execution_contexts(
            staging_dir, real_assets_dir, execution_index,
        )
        raw_results = self._dispatch_tool(
            node.tool, arguments_dicts, workflow, node.name,
            row_contexts, batch_context,
        )
        df = self._build_output_dataframe(raw_results, execution_index, node.tool)
        owned_path_columns = _explicit_template_output_columns(node)
        declared_path_columns = set(templates)
        df = processing_publish(
            workflow.storage_path,
            node.name,
            sig_hash,
            df,
            result_key=result_key,
            attempt_id=attempt_id,
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
        self._emit_progress(
            workflow,
            node.name,
            "completed",
            result_key=result_key,
            record_id=self._selected_record_id(workflow, result_key),
        )
        self._set_node_cache_hit(node, False)
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
        self._normalize_path_arguments(args_dict, input_annotations)
        return Arguments(**args_dict), args_dict

    def _resolve_defaults(
        self, node: Node, input_annotations: dict[str, Any],
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
            normalized[field_name] = normalized[field_name].map(
                _absolute_runtime_path
            )
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
                node, idx, results, input_annotations, index_sets,
            )
            context = self._build_template_context(
                node.name, str(idx), row_args, path_input_fields,
                upstream_nodes, results, idx, timestamp,
            )
            template_assets_dir = assets_dir or _pending_assets_dir(
                workflow.storage_path,
                node.name,
            )
            for out_field, template in templates.items():
                if assets_dir is None:
                    row_args[out_field] = str(template_assets_dir / resolve_template(template, context))
                else:
                    row_args[out_field] = _resolve_staged_output_path(template_assets_dir, template, context)

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
                resolved_idx = idx if str(idx) in idx_set else self._find_parent_index(idx, idx_set)
                if resolved_idx is None:
                    continue
                row_args[field] = _to_python(up_df.at[resolved_idx, col_ref.column])
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
                row_args[out_field] = _resolve_staged_output_path(assets_dir, template, context)
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
            'node_name': node_name,
            'row_index': row_index.replace('::', '_'),
            'timestamp': timestamp or str(int(time.time())),
        }
        for field_name, value in row_args.items():
            context[field_name] = value

        path_values = [
            row_args[pf]
            for pf in path_input_fields
            if pf in row_args and row_args[pf] is not None
        ]
        if len(path_values) == 1:
            context['_ext'] = Path(str(path_values[0])).suffix
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
        """Compute the logical digest for a non-source ProcessingTool."""
        env_hash = compute_env_hash(cast(ProcessingTool, node.tool).environment.dependencies)
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
            'bindings': {f: {'node': cr.node.name, 'column': cr.column}
                         for f, cr in node._column_bindings.items()},
            'constants': signature_constants,
            'defaults': signature_defaults,
            'output_templates': get_output_templates(
                node.tool.Outputs,
                node.tool.Inputs,
                node.output_templates,
            ),
        }
        return self._compute_sig_hash(
            node, env_hash, resolved_params, upstream_hash_map, workflow,
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

    def _dispatch_tool(
        self,
        tool: ProcessingTool,
        arguments_dicts: list[dict[str, Any]],
        workflow: Any,
        node_name: str,
        row_contexts: list[ExecutionContext],
        batch_context: ExecutionContext,
    ) -> list[list[Any]]:
        """Dispatch to process_batch or process_row. Returns list[list[Outputs]]."""
        has_batch = type(tool).process_batch is not ProcessingTool.process_batch

        if self._use_wetlands and self._env_manager is not None:
            return self._dispatch_via_wetlands(tool, arguments_dicts, workflow,
                                               node_name, has_batch,
                                               row_contexts, batch_context)

        return self._dispatch_direct(tool, arguments_dicts, workflow,
                                     node_name, has_batch,
                                     row_contexts, batch_context)

    def _dispatch_direct(
        self,
        tool: ProcessingTool,
        arguments_dicts: list[dict[str, Any]],
        workflow: Any,
        node_name: str,
        has_batch: bool,
        row_contexts: list[ExecutionContext],
        batch_context: ExecutionContext,
    ) -> list[list[Any]]:
        """Direct dispatch — tool runs in the main process."""
        if has_batch:
            if not arguments_dicts and not getattr(tool, "run_empty_batch", False):
                return []
            args_list = [Arguments(**d) for d in arguments_dicts]
            kwargs = {}
            if _accepts_context(tool.process_batch):
                kwargs["context"] = batch_context
            raw_results = tool.process_batch(args_list, **kwargs)
            if raw_results and not isinstance(raw_results[0], list):
                raw_results = [[r] for r in raw_results]
            return raw_results

        raw_results: list[list[Any]] = []
        accepts_context = _accepts_context(tool.process_row)
        for i, (args_dict, context) in enumerate(zip(arguments_dicts, row_contexts)):
            kwargs = {"context": context} if accepts_context else {}
            result = tool.process_row(Arguments(**args_dict), **kwargs)
            if not isinstance(result, list):
                result = [result]
            raw_results.append(result)
            self._emit_progress(workflow, node_name, "row_complete",
                                row=i, total_rows=len(arguments_dicts))
        return raw_results

    def _resolve_worker_config(
        self, tool: ProcessingTool, workflow: Any,
    ) -> tuple[int, Any, float | None]:
        """Determine max_workers, worker_env, and worker_timeout for a tool's environment.

        Resolution order:
        1. Explicit ``get_environment()`` override takes precedence.
        2. GPU auto-inference: if any tool in the environment declares
           ``ResourceSpec(gpu >= 1)`` and no explicit ``worker_env`` was set,
           auto-generate ``worker_env = lambda i: {"CUDA_VISIBLE_DEVICES": str(i)}``.
        3. Fall back to ``Workflow.max_workers``, no ``worker_env``, no ``worker_timeout``.
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
            def wef(i):
                return {"CUDA_VISIBLE_DEVICES": str(i)}
            worker_env = wef
        else:
            worker_env = None

        worker_timeout = env_config.worker_timeout if env_config else None

        return max_workers, worker_env, worker_timeout

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
        row_contexts: list[ExecutionContext],
        batch_context: ExecutionContext,
    ) -> list[list[Any]]:
        """Dispatch through Wetlands — tool runs in isolated environment workers."""
        from bioimageflow.env_manager import _find_tool_file
        from wetlands.task import TaskStatus, TaskEventType

        assert self._env_manager is not None
        if has_batch and not arguments_dicts and not getattr(tool, "run_empty_batch", False):
            return []
        env_spec = tool.environment
        tool_file_path = _find_tool_file(type(tool))
        tool_class_name = type(tool).__name__
        max_workers, worker_env, worker_timeout = self._resolve_worker_config(tool, workflow)
        engine_timeout = _compute_engine_timeout(worker_timeout)

        if has_batch:
            task = self._env_manager.submit_process_batch(
                env_spec, tool_file_path, tool_class_name, arguments_dicts,
                batch_context.to_dict(),
                max_workers=max_workers, worker_env=worker_env,
                worker_timeout=worker_timeout,
            )
            try:
                task.wait_for(timeout=engine_timeout)
            except TimeoutError:
                self._emit_progress(workflow, node_name, "failed")
                task.cancel()
                raise WorkerTimeoutError(
                    f"Batch task for node '{node_name}' exceeded engine-side "
                    f"timeout ({engine_timeout:.0f}s; "
                    f"worker_timeout={worker_timeout}s)"
                )
            if task.status == TaskStatus.FAILED:
                _raise_worker_task_error(
                    task,
                    node_name=node_name,
                    tool=tool,
                    row_index=None,
                )
            if task.status == TaskStatus.CANCELED:
                raise WorkflowCancelledError("Workflow cancelled during batch execution")
            result_dicts = task.result
            assert tool.Outputs is not None
            return [[tool.Outputs(**d) for d in row] for row in result_dicts]

        tasks = self._env_manager.map_process_rows(
            env_spec, tool_file_path, tool_class_name, arguments_dicts,
            [context.to_dict() for context in row_contexts],
            max_workers=max_workers, worker_env=worker_env,
            worker_timeout=worker_timeout,
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

        # Wait and collect results — fail-fast on first error, cancel, or timeout
        try:
            for i, task in enumerate(tasks):
                if workflow.cancel_requested:
                    raise WorkflowCancelledError("Workflow cancelled by user")
                try:
                    task.wait_for(timeout=engine_timeout)
                except TimeoutError:
                    self._emit_progress(workflow, node_name, "failed",
                                        row=i, total_rows=len(tasks))
                    raise WorkerTimeoutError(
                        f"Task for node '{node_name}' row {i} exceeded "
                        f"engine-side timeout ({engine_timeout:.0f}s; "
                        f"worker_timeout={worker_timeout}s)"
                    )
                if task.status == TaskStatus.FAILED:
                    _raise_worker_task_error(
                        task,
                        node_name=node_name,
                        tool=tool,
                        row_index=row_contexts[i].row_index,
                    )
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
                if workflow.cancel_requested:
                    raise WorkflowCancelledError("Workflow cancelled by user")
                df, sig_hash = self._execute_node(inode, results, sig_hashes, workflow)
                cache_hit = self._pop_node_cache_hit(inode)
                self._write_run_node_view(workflow, inode, sig_hash, cache_hit=cache_hit)
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
        result_key: str | None = None,
        record_id: str | None = None,
    ) -> None:
        """Emit a progress event, serialized via ``_progress_lock``."""
        if workflow.on_progress is not None:
            from bioimageflow.workflow import ProgressEvent
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

    def plan(self, workflow: Any) -> dict[str, NodePlan]:
        """Return the cache status and diagnostic plan state of every node.

        Walks the graph in topological order, computes diagnostic logical
        signatures with the same helpers as :meth:`execute`, and reports
        result-key/current-record state when enough upstream cache selections
        are known. No tool code runs, and no Wetlands environment is launched.

        Sub-workflow internal nodes appear under scoped names
        (``"subworkflow_name/internal_name"``), matching
        :meth:`execute_steps`.

        Disabled nodes and nodes downstream of disabled nodes are
        returned with ``skipped=True`` and no final result key.

        Raises
        ------
        CycleInWorkflowError
            If the graph contains a cycle. Use :meth:`Workflow.validate`
            for non-fatal cycle reporting.
        """
        reachable = set(workflow._nodes.values())
        dep_graph = self._build_dep_graph_from_set(reachable)
        try:
            order = list(TopologicalSorter(dep_graph).static_order())
        except CycleError as exc:
            cycle_nodes = exc.args[1] if len(exc.args) > 1 else []
            names = [getattr(n, "name", str(n)) for n in cycle_nodes]
            raise CycleInWorkflowError(names) from exc
        _executable, skipped = self._filter_executable(order)

        plan: dict[str, NodePlan] = {}
        results: dict[Node, Any] = {}
        sig_hashes: dict[Node, str] = {}

        for node in order:
            if node in skipped:
                plan[node.name] = NodePlan(
                    node.name, "", NodePlanStatus.SKIPPED,
                    tuple(self._plan_upstream_names(node)),
                )
                continue
            self._plan_node(node, results, sig_hashes, workflow, plan)

        # Include any nodes not reachable from terminals (shouldn't happen
        # in practice, but plan is expected to cover every registered node).
        for name, node in workflow._nodes.items():
            plan.setdefault(name, NodePlan(
                name, "", NodePlanStatus.SKIPPED, (),
            ))
        return plan

    def _plan_node(
        self,
        node: Node,
        results: dict[Node, Any],
        sig_hashes: dict[Node, str],
        workflow: Any,
        plan: dict[str, NodePlan],
    ) -> None:
        """Compute a NodePlan for a single node, recursing into sub-workflows."""
        from bioimageflow.dataframe_tool import DataFrameTool
        from bioimageflow.sub_workflow import SubWorkflowNode

        if isinstance(node, SubWorkflowNode):
            self._plan_sub_workflow(node, results, sig_hashes, workflow, plan)
            return

        cached_df, sig_hash = self._check_node_cache(
            node, results, sig_hashes, workflow, hydrate_assets=False,
        )
        if sig_hash is None:
            # Non-cacheable top-level node (shouldn't happen outside SubWorkflowNode)
            plan[node.name] = NodePlan(
                node.name, "", NodePlanStatus.UNEXECUTED,
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
        final_result_key = self._plan_final_result_key(node, sig_hash) if not pending_upstreams else None
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

    def _plan_selected_record_id(self, workflow: Any, final_result_key: str | None) -> str | None:
        if final_result_key is None:
            return None
        pointer = Storage(workflow.storage_path).load_current(final_result_key)
        return pointer.record_id if pointer is not None else None

    def _plan_sub_workflow(
        self,
        node: Any,
        results: dict[Node, Any],
        sig_hashes: dict[Node, str],
        workflow: Any,
        plan: dict[str, NodePlan],
    ) -> None:
        """Recursively plan a SubWorkflowNode's internal nodes."""
        from bioimageflow.sub_workflow import SubWorkflowNode

        # Proxy node stands in for the sub-workflow's external inputs.
        proxy_node = node._proxy_node
        sig_hashes[proxy_node] = "proxy"

        internal_nodes = set(node.internal_nodes)
        internal_nodes.add(proxy_node)
        internal_order = self._topological_sort(internal_nodes)

        # Scope names during planning so cache lookups match execute().
        original_names: dict[Node, str] = {}
        for inode in node.internal_nodes:
            original_names[inode] = inode._name
            inode._name = f"{node.name}/{inode._name}"

        try:
            for inode in internal_order:
                if inode is proxy_node:
                    continue
                if isinstance(inode, SubWorkflowNode):
                    self._plan_sub_workflow(inode, results, sig_hashes, workflow, plan)
                    continue
                self._plan_node(inode, results, sig_hashes, workflow, plan)

            # Combined hash — same shape as _execute_sub_workflow.
            import hashlib
            import json
            terminal_hashes = {}
            for field, col_ref in node._output_mapping.items():
                if col_ref.node in sig_hashes:
                    terminal_hashes[field] = sig_hashes[col_ref.node]
            combined = hashlib.sha256(
                json.dumps(terminal_hashes, sort_keys=True).encode()
            ).hexdigest()
            sig_hashes[node] = combined
            # SubWorkflowNode itself isn't cached at this level — its
            # status mirrors the aggregate of internal-node statuses.
            # We surface it as UNEXECUTED unless every internal entry
            # already has a cache hit.
            internal_statuses = [
                plan[inode._name].status
                for inode in node.internal_nodes
                if inode._name in plan
            ]
            if internal_statuses and all(
                s is NodePlanStatus.CACHED for s in internal_statuses
            ):
                sw_status = NodePlanStatus.CACHED
            else:
                sw_status = NodePlanStatus.UNEXECUTED
            plan[node.name] = NodePlan(
                node.name,
                combined,
                sw_status,
                tuple(self._plan_upstream_names(node)),
            )
        finally:
            for inode, orig_name in original_names.items():
                inode._name = orig_name

    def _plan_upstream_names(self, node: Node) -> list[str]:
        names: list[str] = []
        for up in node._upstream_nodes:
            names.append(up.name)
        for arg in node._args:
            if isinstance(arg, Node):
                names.append(arg.name)
        return list(dict.fromkeys(names))


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
        self, tool: ProcessingTool, workflow: Any,
    ) -> tuple[int, Any, float | None]:
        """Always single-worker, no worker_env — truly sequential.

        ``worker_timeout`` is still honored from ``get_environment()`` so a
        hung tool doesn't block the sequential engine indefinitely.
        """
        env_config = workflow._env_configs.get(tool.environment.name)
        worker_timeout = env_config.worker_timeout if env_config else None
        return 1, None, worker_timeout
