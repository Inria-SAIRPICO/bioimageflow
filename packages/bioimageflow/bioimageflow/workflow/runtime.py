"""Focused methods extracted from the workflow façade."""

# Pyright checks the complete contract on Workflow; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false, reportCallIssue=false, reportGeneralTypeIssues=false

from __future__ import annotations

from collections.abc import Generator, Mapping
from typing import TYPE_CHECKING, Literal

from .common import (
    Any,
    EnvironmentSpec,
    Node,
    Path,
    ProcessingTool,
    WorkflowEnvironment,
    datetime,
    hashlib,
    json,
    logger,
    timezone,
)

if TYPE_CHECKING:
    from bioimageflow.engine import DefaultEngine, EnvironmentLifetime, NodeStep
    from bioimageflow.env_manager import WetlandsEnvManager


class _RuntimeMixin:
    def create_engine(
        self,
        *,
        environment_lifetime: "EnvironmentLifetime | str" = "execution",
        env_manager: "WetlandsEnvManager | None" = None,
    ) -> "DefaultEngine":
        """Create an engine preserving this workflow's execution configuration.

        ``environment_lifetime`` controls whether Wetlands workers stop after
        each execution (``"execution"``), remain warm until ``engine.close()``
        (``"engine"``), or are owned entirely by the caller
        (``"external"``). An existing manager can be injected so multiple
        workflows and engines share the same worker environments.
        """
        from bioimageflow.engine import DefaultEngine, SequentialEngine

        use_wetlands = self.engine_type == "wetlands"
        kwargs: dict[str, Any] = {
            "use_wetlands": use_wetlands,
            "wetlands_config": self.wetlands_config,
            "environment_lifetime": environment_lifetime,
            "env_manager": env_manager,
        }
        if self.execution == "sequential":
            return SequentialEngine(**kwargs)
        return DefaultEngine(**kwargs)

    def _make_engine(self) -> "DefaultEngine":
        """Compatibility wrapper for the former private engine factory."""
        return self.create_engine()

    def cancel(self) -> None:
        """Request cancellation of the running workflow."""
        self._cancel_event.set()

    @property
    def cancel_requested(self) -> bool:
        """Whether cancellation has been requested."""
        return self._cancel_event.is_set()

    def get_environment(
        self, target: "ProcessingTool | EnvironmentSpec | str"
    ) -> WorkflowEnvironment:
        """Get the launch configuration proxy for an environment.

        Args:
            target: A ProcessingTool instance, an EnvironmentSpec, or an env name string.

        Returns:
            A shared WorkflowEnvironment proxy. Multiple calls with tools sharing
            the same environment return the same object.
        """
        if isinstance(target, str):
            name = target
            spec = None
        elif isinstance(target, EnvironmentSpec):
            name = target.name
            spec = target
        elif isinstance(target, ProcessingTool):
            name = target.environment.name
            spec = target.environment
        else:
            raise TypeError(
                f"Expected a ProcessingTool, EnvironmentSpec, or str, got {type(target).__name__}"
            )
        if name not in self._env_configs:
            self._env_configs[name] = WorkflowEnvironment(name=name, spec=spec)
        elif spec is not None and self._env_configs[name].spec is None:
            self._env_configs[name].spec = spec
        return self._env_configs[name]

    def compute(
        self,
        *targets: Node,
        inputs: Mapping[str, Any] | None = None,
        dev_mode: bool = False,
        engine: "DefaultEngine | None" = None,
    ) -> Any:
        """Execute the workflow and return results.

        Parameters:
            dev_mode: Development mode flag
            engine: Optional pre-configured engine to use. If None, the configured engine backend and execution policy are used. Providing an engine allows post-execution inspection and testing."""
        self._cancel_event.clear()
        self._dev_mode = dev_mode

        if inputs is not None or not targets:
            supplied = dict(inputs or {})
            parent = type(self)(
                name=f"{self.name}-execution",
                storage_path=self.storage_path,
                engine=self.engine_type,
                execution=self.execution,
                on_progress=self.on_progress,
                wetlands_config=self.wetlands_config,
                max_workers=self.max_workers,
                output_view=self.output_view,
            )
            parent._accept_root_dataframes = True
            with parent:
                boundary = self(name=self.name, **supplied)
            boundary._is_root_boundary = True
            return parent.compute(boundary, dev_mode=dev_mode, engine=engine)

        # If targets are not registered (explicit workflow without context manager),
        # discover the graph by tracing upstream
        target_list = list(targets)
        self._discover_graph(target_list)

        if engine is None:
            engine = self._make_engine()
        self._start_run_view(target_list)
        try:
            results = engine.execute(target_list, self)
        except Exception as exc:
            self._finish_run_view(
                self._run_status_for_exception(exc),
                update_latest_success=False,
            )
            raise
        else:
            self._finish_run_view("succeeded", update_latest_success=True)

        if len(target_list) == 1:
            return list(results.values())[0]
        return results

    def compute_steps(
        self,
        *targets: Node,
        inputs: Mapping[str, Any] | None = None,
        dev_mode: bool = False,
        engine: "DefaultEngine | None" = None,
    ) -> "Generator[NodeStep, None, None]":
        """Execute the workflow step by step, yielding a :class:`NodeStep`
        for each node in topological (dependency) order.

        Parameters:
            dev_mode: Development mode flag
            engine: Optional pre-configured engine to use. If None, a default DefaultEngine is created.

        The engine stays alive between yields so Wetlands environments
        remain warm — ideal for interactive debugging.

        Usage::

            for step in wf.compute_steps(results):
                print(f"Next: {step.node_name}")
                step.prepare()     # optional: launches env — attach debugger here
                df = step.execute()
                print(df.head())

        If ``step.execute()`` is not called before advancing to the next
        iteration, the step auto-executes to keep downstream nodes consistent.
        """
        self._dev_mode = dev_mode

        if inputs is not None or not targets:
            supplied = dict(inputs or {})
            parent = type(self)(
                name=f"{self.name}-execution",
                storage_path=self.storage_path,
                engine=self.engine_type,
                execution=self.execution,
                on_progress=self.on_progress,
                wetlands_config=self.wetlands_config,
                max_workers=self.max_workers,
                output_view=self.output_view,
            )
            parent._accept_root_dataframes = True
            with parent:
                boundary = self(name=self.name, **supplied)
            boundary._is_root_boundary = True
            yield from parent.compute_steps(
                boundary,
                dev_mode=dev_mode,
                engine=engine,
            )
            return

        target_list = list(targets)
        self._discover_graph(target_list)

        if engine is None:
            engine = self._make_engine()
        self._start_run_view(target_list)
        try:
            yield from engine.execute_steps(target_list, self)
        except GeneratorExit:
            self._finish_run_view("cancelled", update_latest_success=False)
            raise
        except Exception as exc:
            self._finish_run_view(
                self._run_status_for_exception(exc),
                update_latest_success=False,
            )
            raise
        else:
            self._finish_run_view("succeeded", update_latest_success=True)

    def export_outputs(
        self,
        *,
        mode: Literal["pointer", "symlink", "copy", "hardlink"] = "symlink",
        scope: Literal["latest", "runs", "both"] = "latest",
        run_id: str | None = None,
    ) -> list[Path]:
        """Materialize human-facing output files from the portable JSON views."""
        if scope not in {"latest", "runs", "both"}:
            raise ValueError(
                "Invalid output_view scope. Expected 'latest', 'runs', or 'both'."
            )
        from bioimageflow.storage import CacheCorruptionError, Storage

        storage = Storage(self.storage_path)
        materialized: list[Path] = []
        if scope in {"latest", "both"}:
            materialized.extend(storage.materialize_latest_outputs(mode))
        if scope in {"runs", "both"}:
            selected_run_id = run_id or storage.latest_success_run_id()
            if selected_run_id is None:
                raise CacheCorruptionError(
                    "No successful run view is available for output export."
                )
            materialized.extend(storage.materialize_run_outputs(selected_run_id, mode))
        return materialized

    def _auto_export_outputs(
        self,
        run_id: str,
        *,
        latest_node: str | None = None,
        runs: bool,
    ) -> None:
        output_view = self.output_view
        if output_view is None or output_view.mode == "none":
            return
        from bioimageflow.storage import Storage

        storage = Storage(self.storage_path)
        try:
            if latest_node is not None and output_view.scope in {"latest", "both"}:
                storage.materialize_latest_node_outputs(latest_node, output_view.mode)
            if runs and output_view.scope in {"runs", "both"}:
                storage.materialize_run_outputs(run_id, output_view.mode)
        except Exception:
            logger.warning(
                "Automatic output-view materialization failed (mode=%s, scope=%s, run_id=%s).",
                output_view.mode,
                output_view.scope,
                run_id,
                exc_info=True,
            )

    def _start_run_view(self, targets: list[Node]) -> None:
        from bioimageflow.storage import Storage

        storage = Storage(self.storage_path)
        run_id = f"run_{storage.new_attempt_id()}"
        started_at = datetime.now(timezone.utc).isoformat()
        target_nodes = [target.name for target in targets]
        self._run_view_context = {
            "run_id": run_id,
            "started_at": started_at,
            "target_nodes": target_nodes,
        }
        storage.write_run_metadata(
            run_id,
            workflow_identity=self._workflow_identity(target_nodes),
            engine=f"{self.engine_type}:{self.execution}",
            status="running",
            target_nodes=target_nodes,
            started_at=started_at,
        )

    def _finish_run_view(self, status: str, *, update_latest_success: bool) -> None:
        context = self._run_view_context
        self._run_view_context = None
        if context is None:
            return
        from bioimageflow.storage import Storage

        storage = Storage(self.storage_path)
        run_id = str(context["run_id"])
        storage.write_run_metadata(
            run_id,
            workflow_identity=self._workflow_identity(list(context["target_nodes"])),
            engine=f"{self.engine_type}:{self.execution}",
            status=status,
            target_nodes=list(context["target_nodes"]),
            started_at=str(context["started_at"]),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        if update_latest_success:
            storage.update_latest_success_run(run_id)
        if status == "succeeded":
            self._auto_export_outputs(run_id, latest_node=None, runs=True)

    def _workflow_identity(self, target_nodes: list[str]) -> str:
        payload = {
            "nodes": sorted(self._nodes),
            "targets": list(target_nodes),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        return f"workflow:{digest}"

    def _run_status_for_exception(self, exc: Exception) -> str:
        from bioimageflow.engine import WorkflowCancelledError

        return "cancelled" if isinstance(exc, WorkflowCancelledError) else "failed"

    def _discover_graph(self, targets: list[Node]) -> None:
        """Discover and register all nodes reachable from targets."""
        visited: set[str] = set()
        queue = list(targets)
        while queue:
            node = queue.pop(0)
            if node.name in visited:
                continue
            visited.add(node.name)
            if node.name not in self._nodes:
                self._nodes[node.name] = node
            for up in node._upstream_nodes:
                queue.append(up)
            for arg in node._args:
                if isinstance(arg, Node):
                    queue.append(arg)
