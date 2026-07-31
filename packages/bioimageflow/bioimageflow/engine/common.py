"""Shared values and helpers used by execution-engine modules."""

# This module provides shared definitions to focused engine modules.
# ruff: noqa: F401

import concurrent.futures
import hashlib
import inspect
import json
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
    deterministic_serialize,
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
from bioimageflow.node import IndexAlignmentError, Node, scoped_node_names
from bioimageflow.storage import (
    CacheCorruptionError,
    Storage,
    canonical_dataframe_digest,
    validate_relative_posix_path,
)
from bioimageflow.template import get_output_templates, resolve_template
from bioimageflow.validation import get_tool_version, get_source_hash, is_path_type
from bioimageflow.backends import (
    DirectBackend,
    ProcessingDispatch,
    ProcessingBackend,
    WetlandsBackend,
)

if TYPE_CHECKING:
    from bioimageflow.env_manager import WetlandsEnvManager
    from bioimageflow.workflow_node import WorkflowNode
    from .scheduler import DefaultEngine

logger = logging.getLogger("bioimageflow")


class ResourceLifetime(str, Enum):
    """Ownership policy for resources used by an execution engine."""

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
        if field_name not in signature_constants and hasattr(
            node.tool.Inputs, field_name
        ):
            signature_constants[field_name] = getattr(node.tool.Inputs, field_name)
        if field_name in signature_constants and is_path_type(annotation):
            signature_constants[field_name] = _absolute_runtime_path(
                signature_constants[field_name]
            )
    return {
        "constants": signature_constants,
        "output_templates": templates,
    }


def _resolve_staged_output_path(
    assets_dir: Path, template: str, context: dict[str, Any]
) -> str:
    resolved = resolve_template(template, context)
    try:
        relative = validate_relative_posix_path(Path(resolved).as_posix())
    except ValueError as exc:
        raise CacheCorruptionError(
            f"Unsafe output template path: {resolved!r}"
        ) from exc
    path = assets_dir / relative
    try:
        path.resolve().relative_to(assets_dir.resolve())
    except ValueError as exc:
        raise CacheCorruptionError(
            f"Output template path escapes staging assets: {resolved!r}"
        ) from exc
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
        raise TypeError(
            f"{type(tool).__name__}.zero_row_scalar_outputs must be a dict."
        )
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
        return any(
            _is_shared_array_type(arg)
            for arg in get_args(annotation)
            if arg is not type(None)
        )
    return annotation is SharedArray


def _has_shared_array_output(tool: Any) -> bool:
    outputs = getattr(tool, "Outputs", None)
    if outputs is None or not hasattr(outputs, "_get_all_annotations"):
        return False
    return any(
        _is_shared_array_type(annotation)
        for annotation in outputs._get_all_annotations().values()
    )


def _shared_array_output_columns(tool: Any) -> set[str]:
    outputs = getattr(tool, "Outputs", None)
    if outputs is None or not hasattr(outputs, "_get_all_annotations"):
        return set()
    return {
        name
        for name, annotation in outputs._get_all_annotations().items()
        if _is_shared_array_type(annotation)
    }


def _processing_has_other_current(
    storage_path: str | Path, node_name: str, sig_hash: str
) -> bool:
    expected_key = processing_result_key(node_name, sig_hash)
    for metadata in iter_processing_result_metadata(storage_path):
        if (
            metadata.get("node") != node_name
            or metadata.get("result_key") == expected_key
        ):
            continue
        return True
    return False


def _dataframe_has_other_current(
    storage_path: str | Path, node_name: str, sig_hash: str
) -> bool:
    expected_key = dataframe_result_key(node_name, sig_hash)
    for metadata in iter_dataframe_result_metadata(storage_path):
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
        Scoped node name (``"workflow-node/internal"`` for nested workflow
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
        task_status=getattr(task, "state", None),
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
        sig_hashes: dict[Node, str | None],
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
        self._engine._backend.prepare_node(
            self._engine,
            self._node,
            self._workflow,
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
                    record_id=self._engine._selected_record_id(
                        self._workflow, result_key
                    ),
                )
            self._executed = True
            return self._df
        df, sig_hash = self._engine._execute_node(
            self._node,
            self._results,
            self._sig_hashes,
            self._workflow,
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
            self._node,
            self._results,
            self._sig_hashes,
            self._workflow,
        )
        self._cached_df = cached_df
        if sig_hash is not None:
            self._sig_hash = sig_hash
