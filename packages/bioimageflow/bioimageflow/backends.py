"""Backend-neutral processing dispatch adapters.

The scheduler owns graph, cache, publication, and progress semantics. A
backend only prepares and executes an already-resolved ``ProcessingTool``
invocation. Keeping this boundary small lets additional execution backends be
added without duplicating orchestration behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from bioimageflow_core import ExecutionContext, ProcessingTool

if TYPE_CHECKING:
    from bioimageflow.engine import DefaultEngine
    from bioimageflow.node import Node


@dataclass(frozen=True)
class ProcessingDispatch:
    """Immutable resolved input passed from the scheduler to one backend."""

    tool: ProcessingTool
    arguments: tuple[dict[str, Any], ...]
    workflow: Any
    node_name: str
    row_contexts: tuple[ExecutionContext, ...]
    batch_context: ExecutionContext
    has_batch: bool


class ProcessingBackend(Protocol):
    """Internal execution-backend contract used by ``DefaultEngine``."""

    def prepare_node(
        self,
        engine: "DefaultEngine",
        node: "Node",
        workflow: Any,
    ) -> None:
        """Prepare backend resources for an uncached node."""
        ...

    def dispatch(
        self,
        engine: "DefaultEngine",
        request: ProcessingDispatch,
    ) -> list[list[Any]]:
        """Execute one completely resolved processing request."""
        ...

    def cleanup_execution(self, engine: "DefaultEngine") -> None:
        """Release resources owned for one execution."""
        ...

    def close(self, engine: "DefaultEngine") -> None:
        """Release resources owned for the engine lifetime."""
        ...


class DirectBackend:
    """Execute tool methods in the orchestrator process."""

    def prepare_node(
        self,
        engine: "DefaultEngine",
        node: "Node",
        workflow: Any,
    ) -> None:
        del engine, node, workflow

    def dispatch(
        self,
        engine: "DefaultEngine",
        request: ProcessingDispatch,
    ) -> list[list[Any]]:
        return engine._dispatch_direct(
            request.tool,
            list(request.arguments),
            request.workflow,
            request.node_name,
            request.has_batch,
            list(request.row_contexts),
            request.batch_context,
        )

    def cleanup_execution(self, engine: "DefaultEngine") -> None:
        del engine

    def close(self, engine: "DefaultEngine") -> None:
        del engine


class WetlandsBackend:
    """Dispatch tool methods through the configured Wetlands manager."""

    def prepare_node(
        self,
        engine: "DefaultEngine",
        node: "Node",
        workflow: Any,
    ) -> None:
        if engine._env_manager is None:
            return
        tool = node.tool
        if not isinstance(tool, ProcessingTool):
            return
        max_workers, worker_env, worker_timeout = engine._resolve_worker_config(
            tool,
            workflow,
        )
        engine._env_manager.get_or_create(
            tool.environment,
            max_workers=max_workers,
            worker_env=worker_env,
            worker_timeout=worker_timeout,
        )

    def dispatch(
        self,
        engine: "DefaultEngine",
        request: ProcessingDispatch,
    ) -> list[list[Any]]:
        return engine._dispatch_via_wetlands(
            request.tool,
            list(request.arguments),
            request.workflow,
            request.node_name,
            request.has_batch,
            list(request.row_contexts),
            request.batch_context,
        )

    def cleanup_execution(self, engine: "DefaultEngine") -> None:
        from bioimageflow.engine import ResourceLifetime

        if (
            engine.resource_lifetime is ResourceLifetime.EXECUTION
            and engine._env_manager is not None
        ):
            engine._env_manager.shutdown_all()

    def close(self, engine: "DefaultEngine") -> None:
        from bioimageflow.engine import ResourceLifetime

        if (
            engine._env_manager is not None
            and engine.resource_lifetime is not ResourceLifetime.EXTERNAL
        ):
            engine._env_manager.shutdown_all()
