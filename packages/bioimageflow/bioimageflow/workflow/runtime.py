"""Focused methods extracted from the workflow façade."""

# Pyright checks the complete contract on Workflow; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false, reportCallIssue=false, reportGeneralTypeIssues=false

from __future__ import annotations

import importlib
from collections.abc import Generator, Iterator, Mapping
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
from .execution_context import WorkflowExecutionContext

if TYPE_CHECKING:
    from bioimageflow.engine import NodeStep, ResourceLifetime
    from bioimageflow.env_manager import WetlandsEnvManager
    from typing import Protocol

    class _ExecutionEngine(Protocol):
        @property
        def backend_name(self) -> str: ...

        def execute(self, targets: Any, workflow: Any) -> Any: ...

        def execute_steps(
            self,
            targets: Any,
            workflow: Any,
        ) -> Iterator["NodeStep"]: ...


class _WorkflowSteps(Iterator["NodeStep"]):
    """Iterator that reserves and releases a Workflow execution eagerly."""

    def __init__(
        self,
        workflow: Any,
        iterator: Generator["NodeStep", None, None],
        reserved_engine: Any | None = None,
    ) -> None:
        self._workflow = workflow
        self._iterator = iterator
        self._reserved_engine = reserved_engine
        self._closed = False

    def __next__(self) -> "NodeStep":
        if self._closed:
            raise StopIteration
        try:
            return next(self._iterator)
        except StopIteration:
            self._release()
            raise
        except BaseException:
            self._release()
            raise

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._iterator.close()
        finally:
            self._release()

    def _release(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reserved_engine is not None:
            self._workflow._release_engine_execution_reservation(
                self._reserved_engine
            )
        self._workflow._end_public_execution()


class _RuntimeMixin:
    def create_engine(
        self,
        *,
        resource_lifetime: "ResourceLifetime | str" = "execution",
        env_manager: "WetlandsEnvManager | None" = None,
        parsl_config: Any = None,
        dfk: Any = None,
        executor_bindings: Mapping[str, Any] | None = None,
        parsl_node_routes: Mapping[str, str] | None = None,
        parsl_environment_routes: Mapping[str, str] | None = None,
        parsl_shared_runtime_root: str | Path | None = None,
        parsl_execution: Literal["workflow", "parallel", "sequential"] = "workflow",
        parsl_task_policy: Any = None,
    ) -> "_ExecutionEngine":
        """Create the configured direct, Wetlands, or Parsl engine."""
        from bioimageflow.engine import (
            DefaultEngine,
            ResourceLifetime,
            SequentialEngine,
        )

        parsl_values = {
            "parsl_config": parsl_config,
            "dfk": dfk,
            "executor_bindings": executor_bindings,
            "parsl_node_routes": parsl_node_routes,
            "parsl_environment_routes": parsl_environment_routes,
            "parsl_shared_runtime_root": parsl_shared_runtime_root,
            "parsl_task_policy": parsl_task_policy,
        }
        supplied_parsl = [
            name for name, value in parsl_values.items() if value is not None
        ]
        if parsl_execution != "workflow":
            supplied_parsl.append("parsl_execution")

        if self.engine_type == "parsl":
            if env_manager is not None:
                raise ValueError("env_manager is not valid for the Parsl backend.")
            if executor_bindings is None:
                raise ValueError(
                    "The Parsl backend requires executor_bindings. Construct "
                    "an attached engine with "
                    "ParslEngine(parsl_config=config, "
                    "executor_bindings=bindings) and pass engine=engine to "
                    "compute() or compute_steps()."
                )
            if (parsl_config is None) == (dfk is None):
                raise ValueError(
                    "The Parsl backend requires exactly one of parsl_config "
                    "and dfk. Construct an attached engine with "
                    "ParslEngine(parsl_config=config, "
                    "executor_bindings=bindings) and pass engine=engine to "
                    "compute() or compute_steps()."
                )
            ParslEngine = importlib.import_module("bioimageflow.parsl").ParslEngine

            execution = (
                self.execution if parsl_execution == "workflow" else parsl_execution
            )
            return ParslEngine(
                parsl_config=parsl_config,
                dfk=dfk,
                executor_bindings=executor_bindings,
                node_routes=parsl_node_routes,
                environment_routes=parsl_environment_routes,
                shared_runtime_root=parsl_shared_runtime_root,
                execution=execution,
                task_policy=parsl_task_policy,
                resource_lifetime=resource_lifetime,
            )

        if supplied_parsl:
            raise ValueError(
                f"Parsl arguments are invalid for engine='{self.engine_type}': "
                f"{', '.join(sorted(supplied_parsl))}."
            )
        if self.engine_type == "direct":
            if env_manager is not None:
                raise ValueError("env_manager is valid only for the Wetlands backend.")
            try:
                lifetime = ResourceLifetime(resource_lifetime)
            except ValueError as exc:
                raise ValueError(
                    f"Unknown resource_lifetime {resource_lifetime!r}."
                ) from exc
            if lifetime is not ResourceLifetime.EXECUTION:
                raise ValueError(
                    "Direct execution accepts only "
                    "resource_lifetime='execution'."
                )
            kwargs: dict[str, Any] = {
                "resource_lifetime": lifetime,
            }
            if self.execution == "sequential":
                return SequentialEngine(**kwargs)
            return DefaultEngine(**kwargs)

        use_wetlands = self.engine_type == "wetlands"
        kwargs = {
            "use_wetlands": use_wetlands,
            "wetlands_config": self.wetlands_config,
            "resource_lifetime": resource_lifetime,
            "env_manager": env_manager,
        }
        if self.execution == "sequential":
            return SequentialEngine(**kwargs)
        return DefaultEngine(**kwargs)

    def cancel(self) -> None:
        """Request cancellation of the running workflow."""
        with self._execution_lock:
            context = self._active_run_context
        if context is not None:
            context.request_cancel()

    @property
    def cancel_requested(self) -> bool:
        """Whether cancellation has been requested."""
        with self._execution_lock:
            context = self._active_run_context
        return bool(context is not None and context.cancel_requested)

    def _begin_public_execution(
        self,
        context: WorkflowExecutionContext,
    ) -> None:
        with self._execution_lock:
            if self._active_run_context is not None:
                raise RuntimeError("This Workflow already has an active execution.")
            self._active_run_context = context

    def _end_public_execution(self) -> None:
        with self._execution_lock:
            self._active_run_context = None

    @staticmethod
    def _reserve_engine_execution(engine: Any) -> bool:
        reserve = getattr(engine, "_reserve_execution", None)
        if not callable(reserve):
            return False
        reserve()
        return True

    @staticmethod
    def _release_engine_execution_reservation(engine: Any) -> None:
        release = getattr(engine, "_release_execution_reservation", None)
        if callable(release):
            release()

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
        engine: "_ExecutionEngine | None" = None,
        run_context: WorkflowExecutionContext | None = None,
    ) -> Any:
        """Execute the workflow and return results."""
        context = run_context or WorkflowExecutionContext()
        self._begin_public_execution(context)
        try:
            return self._compute_bound(
                targets,
                inputs=inputs,
                dev_mode=dev_mode,
                engine=engine,
                run_context=context,
            )
        finally:
            self._end_public_execution()

    def _compute_bound(
        self,
        targets: tuple[Node, ...],
        *,
        inputs: Mapping[str, Any] | None,
        dev_mode: bool,
        engine: "_ExecutionEngine | None",
        run_context: WorkflowExecutionContext,
    ) -> Any:
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
            parent._active_run_context = run_context
            try:
                return parent._compute_bound(
                    (boundary,),
                    inputs=None,
                    dev_mode=dev_mode,
                    engine=engine,
                    run_context=run_context,
                )
            finally:
                parent._active_run_context = None

        # If targets are not registered (explicit workflow without context manager),
        # discover the graph by tracing upstream
        target_list = list(targets)
        self._discover_graph(target_list)

        if engine is None:
            engine = self.create_engine()
        engine_reserved = self._reserve_engine_execution(engine)
        try:
            self._start_run_view(
                target_list,
                run_context=run_context,
                engine=engine,
            )
            try:
                results = engine.execute(target_list, self)
            except BaseException as exc:
                run_context._execution_failed(exc)
                raise
            else:
                run_context._execution_succeeded()
        finally:
            if engine_reserved:
                self._release_engine_execution_reservation(engine)

        if len(target_list) == 1:
            return list(results.values())[0]
        return results

    def compute_steps(
        self,
        *targets: Node,
        inputs: Mapping[str, Any] | None = None,
        dev_mode: bool = False,
        engine: "_ExecutionEngine | None" = None,
        run_context: WorkflowExecutionContext | None = None,
    ) -> "_WorkflowSteps":
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
        context = run_context or WorkflowExecutionContext()
        self._begin_public_execution(context)
        engine_reserved = False
        try:
            if engine is not None:
                engine_reserved = self._reserve_engine_execution(engine)
            iterator = self._compute_steps_bound(
                targets,
                inputs=inputs,
                dev_mode=dev_mode,
                engine=engine,
                run_context=context,
                engine_reserved=engine_reserved,
            )
            return _WorkflowSteps(
                self,
                iterator,
                reserved_engine=engine if engine_reserved else None,
            )
        except BaseException:
            if engine_reserved and engine is not None:
                self._release_engine_execution_reservation(engine)
            self._end_public_execution()
            raise

    def _compute_steps_bound(
        self,
        targets: tuple[Node, ...],
        *,
        inputs: Mapping[str, Any] | None,
        dev_mode: bool,
        engine: "_ExecutionEngine | None",
        run_context: WorkflowExecutionContext,
        engine_reserved: bool,
    ) -> "Generator[NodeStep, None, None]":
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
            parent._active_run_context = run_context
            try:
                yield from parent._compute_steps_bound(
                    (boundary,),
                    inputs=None,
                    dev_mode=dev_mode,
                    engine=engine,
                    run_context=run_context,
                    engine_reserved=engine_reserved,
                )
                return
            finally:
                parent._active_run_context = None

        target_list = list(targets)
        self._discover_graph(target_list)

        if engine is None:
            engine = self.create_engine()
        if not engine_reserved:
            engine_reserved = self._reserve_engine_execution(engine)
        try:
            self._start_run_view(
                target_list,
                run_context=run_context,
                engine=engine,
            )
            try:
                yield from engine.execute_steps(target_list, self)
            except GeneratorExit:
                from bioimageflow.engine import WorkflowCancelledError

                run_context._execution_failed(
                    WorkflowCancelledError("Workflow step execution was closed.")
                )
                raise
            except BaseException as exc:
                run_context._execution_failed(exc)
                raise
            else:
                run_context._execution_succeeded()
        finally:
            if engine_reserved:
                self._release_engine_execution_reservation(engine)

    def export_outputs(
        self,
        *,
        mode: Literal["pointer", "symlink", "copy", "hardlink"] = "symlink",
        scope: Literal["latest", "runs", "both"] = "latest",
        run_id: str | None = None,
    ) -> list[Path]:
        """Materialize assets, dataframes, and provenance from portable run views."""
        from bioimageflow.storage import export_outputs

        return export_outputs(
            self.storage_path,
            mode=mode,
            scope=scope,
            run_id=run_id,
        )

    def _auto_export_outputs(
        self,
        run_id: str,
        *,
        latest_node: str | None = None,
        runs: bool,
        latest_all: bool = False,
    ) -> None:
        output_view = self.output_view
        if output_view is None or output_view.mode == "none":
            return
        from bioimageflow.storage import Storage

        storage = Storage(self.storage_path)
        try:
            if latest_node is not None and output_view.scope in {"latest", "both"}:
                storage.materialize_latest_node_outputs(latest_node, output_view.mode)
            if latest_all and output_view.scope in {"latest", "both"}:
                storage.materialize_latest_outputs(output_view.mode)
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

    def _start_run_view(
        self,
        targets: list[Node],
        *,
        run_context: WorkflowExecutionContext,
        engine: Any,
    ) -> None:
        from bioimageflow.storage import Storage

        storage = Storage(self.storage_path)
        started_at = datetime.now(timezone.utc).isoformat()
        target_nodes = [target.name for target in targets]

        def finish_success() -> None:
            self._finish_run_view("succeeded", update_latest_success=True)

        def finish_failure(error: BaseException) -> None:
            self._finish_run_view(
                self._run_status_for_exception(error),
                update_latest_success=False,
            )

        run_id = run_context._bind(
            self,
            on_success=finish_success,
            on_failure=finish_failure,
        )
        self._run_view_context = {
            "run_id": run_id,
            "started_at": started_at,
            "target_nodes": target_nodes,
            "engine": engine.backend_name,
            "execution": (
                self.execution
                if getattr(engine, "execution", "workflow") == "workflow"
                else getattr(engine, "execution", self.execution)
            ),
        }
        storage.write_run_metadata(
            run_id,
            workflow_identity=self._workflow_identity(target_nodes),
            engine=f"{self._run_view_context['engine']}:{self._run_view_context['execution']}",
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
            engine=f"{context['engine']}:{context['execution']}",
            status=status,
            target_nodes=list(context["target_nodes"]),
            started_at=str(context["started_at"]),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        if update_latest_success:
            storage.update_latest_success_run(run_id)
        self._auto_export_outputs(
            run_id,
            latest_node=None,
            runs=status == "succeeded",
            latest_all=True,
        )

    def _workflow_identity(self, target_nodes: list[str]) -> str:
        payload = {
            "nodes": sorted(self._nodes),
            "targets": list(target_nodes),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        return f"workflow:{digest}"

    def _run_status_for_exception(self, exc: BaseException) -> str:
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
