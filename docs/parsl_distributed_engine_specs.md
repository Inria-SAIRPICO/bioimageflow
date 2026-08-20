# BioImageFlow Distributed Parsl Engine Specification

Status: normative specification for attached Parsl execution and the submitted-run control plane.

The normative dependencies of this document are:

- `docs/source/specs.md` for the BioImageFlow library contract.
- `docs/source/reference/unified_workflow_contract.md` for recursive workflows, stable interface IDs, graph/archive formats, and scoped execution identifiers.
- `docs/source/reference/output_cache_storage.md` for cache records, publication, runtime path rehydration, run views, and output views.

If this document conflicts with one of those contracts, the platform contract wins and this document must be corrected.

This specification adds a distributed execution backend and launcher control plane.
It does not define a second workflow model, cache, provenance model, tool API, or scheduler.

Requirements use the following terms:

- **MUST**: required for the milestone that contains the requirement.
- **SHOULD**: required unless a documented implementation constraint justifies a different design with equivalent observable behavior.
- **MAY**: optional behavior.

---

## 1. Normative Model

BioImageFlow has one recursive `Workflow` / `WorkflowNode` graph contract, one scheduler, one cache and provenance model, and one `ProcessingTool` worker protocol.
Parsl is a processing backend for that model, not a second workflow system.

Parsl configuration is runtime-only state.
Attached callers inject a live `ParslEngine`; submitted callers provide a JSON-safe configuration-factory reference to the launcher.

Workers resolve one strict installed-module, versioned-module, shared-module, source-file, or archive-module origin.
Wetlands and Parsl use the same core envelope, origin resolver, processing entry point, and worker-instance identity.

The canonical storage contract owns cache identity, immutable records, run views, output views, transient workspaces, path rehydration, and publication.
Attached execution and submitted orchestration share the BioImageFlow run ID but keep launcher control artifacts outside portable run views.

Phase 1 executor environments are preinstalled and explicitly attested.
Parsl providers allocate workers; no compute task installs packages or starts a nested Wetlands worker.

---

## 2. Goals and Phase Boundaries

### 2.1 Phase 1a: attached shared-filesystem engine

Phase 1a MUST provide:

- Explicit attached execution through `workflow.compute(..., engine=ParslEngine(...))` and `workflow.compute_steps(..., engine=ParslEngine(...))`.
- `"parsl"` as a valid workflow engine preference and support in `Workflow.create_engine()`.
- Lazy import of the optional Parsl dependency.
- Remote execution of reachable `ProcessingTool` calls through Parsl Python apps.
- Local orchestrator execution of every `DataFrameTool` and every `WorkflowNode` boundary.
- The normative recursive compilation, disabled-node, index-lineage, output-DataFrame, empty-batch, zero-row, cache, run-view, output-view, progress, and cancellation contracts.
- Shared-filesystem task paths only.
- Explicit executor-to-environment compatibility declarations and executor preflight checks.
- Installed-module, versioned-module, shared-module/source-file, and materialized-archive-module tool origins.
- Rejection of `SharedArray` values that would cross a Parsl task boundary.
- Resource-lifetime values `"execution"`, `"engine"`, and `"external"` with one backend-neutral ownership meaning.

Phase 1a MUST NOT require:

- a submitted orchestrator process,
- scheduler-specific launcher adapters,
- no-shared-filesystem artifact transfer,
- remote `DataFrameTool`,
- downstream streaming before an upstream node is finalized,
- package installation inside compute tasks,
- nested Wetlands task dispatch,
- worker-side cooperative cancellation,
- opaque Parsl retries,
- direct BioImageFlow submission of row or node work to a site scheduler.

### 2.2 Phase 1b: shared launcher and reconnectable run

Phase 1b MUST add:

- one shared launcher used by CLI, GUI, notebooks, and services,
- local submitted mode,
- manual submitted mode,
- stable run IDs allocated before process launch,
- root workflow inputs and ad hoc top-level target selection,
- status, progress, log, cancellation, and result persistence,
- reconnection after the submitting client exits,
- persisted public return DataFrames, including aggregate workflow-boundary results with no cache record.

### 2.3 Later phases

Later work MAY add:

- no-shared-filesystem staging and durable URI support,
- transferable custom-source bundles and package provisioning,
- scheduler-specific orchestrator launcher adapters,
- additional Parsl executor resource adapters,
- remote `DataFrameTool` under a separately approved platform contract,
- streaming partial dataflow.

Parsl providers remain responsible for worker allocation in every phase.
BioImageFlow launcher adapters only start the orchestrator process.

---

## 3. Platform Invariants

The distributed implementation follows these platform invariants.

### 3.1 Package boundary

- The orchestrator and first-party tool packages target Python `>=3.10`.
- `bioimageflow-core` targets Python `>=3.9` and is the only BioImageFlow package required in a `ProcessingTool` worker.
- `bioimageflow-core` includes NumPy, but does not include pandas, Pydantic, Parsl, or orchestrator graph code.
- New worker envelopes and entry points MUST live in `bioimageflow-core` or use plain worker-safe values.
- Worker entry points MUST NOT import pandas, Pydantic, or Parsl.
- New public orchestrator types MUST be exported explicitly through `bioimageflow.__all__`.

### 3.2 Tool execution boundary

- `ProcessingTool.process_row()` and `process_batch()` are the processing execution boundary.
- Direct execution calls them on the graph-construction tool instance in the orchestrator; Wetlands and Parsl call them on an instance created in an isolated worker environment.
- `DataFrameTool.merge_dataframes()` and `transform()` execute in the orchestrator.
- The orchestrator resolves inputs, defaults, absolute runtime paths, output templates, and `ExecutionContext` before dispatch.
- An isolated worker receives resolved `Arguments` and an optional `ExecutionContext`; it does not inspect the workflow graph.
- An isolated worker instantiates the tool class locally and may retain worker-local tool state between calls.
- Wetlands and Parsl MUST NOT serialize the graph-construction tool instance as the worker execution object.

### 3.3 Recursive workflow boundary

- `Workflow` is the only workflow definition type.
- Calling a workflow in a distinct active parent creates a `WorkflowNode` snapshot.
- Compilation expands nested workflows into real tool nodes with scoped names such as `outer/inner/tool`.
- Every enabled internal terminal is a completion dependency, including detached branches.
- Published output providers contribute boundary values and, under the normative storage contract, downstream cache identity through their selected immutable record references and declared selectors.
- Completion-only dependencies must finish, fail, or cancel the boundary, but do not invalidate unrelated consumers when published outputs are unchanged.
- A `WorkflowNode` boundary assembles a DataFrame in the orchestrator and owns no cache record.
- A boundary diagnostic signature is not a reusable result key and MUST NOT be used as one.

- A zero-output workflow executes its enabled terminals and returns a canonical zero-row, zero-column DataFrame.

### 3.4 Cache and run boundary

- Result keys and selected immutable record IDs are the cache identity.
- `current.json` uses the guarded `first-valid` policy.
- Attempt directories are mutable and never reusable records.
- A published record is immutable and contains `manifest.json` plus mandatory `dataframe.parquet`.
- `manifest.outputs` is the artifact metadata authority.
- Run views live under `views/runs/`; per-node latest views live under `views/latest/`; optional materialized projections live under `outputs/`.
- Workflow-node aggregate plan entries and returned boundary DataFrames do not imply aggregate cache records.

---

## 4. Architecture and Responsibility Split

```text
client / platform
  -> BioImageFlow launcher (submitted mode only)
  -> BioImageFlow orchestrator process
       - recursive graph compilation
       - validation and planning
       - DataFrameTool execution
       - argument and template resolution
       - Parsl task submission and collection
       - cache publication and run/output views
       - public progress and cancellation
  -> Parsl DataFlowKernel
       - executor selection
       - worker-block allocation through providers
       - task transport and execution
  -> persistent Parsl worker processes
       - bioimageflow-core worker entry point
       - ProcessingTool dependencies
       - worker-local tool instances and model state
```

The launcher owns:

- allocating and persisting the run control state,
- starting or describing the orchestrator process,
- tracking a local or scheduler backend identifier,
- relaying cancellation requests,
- exposing reconnectable status, progress, logs, and results.

The BioImageFlow orchestrator owns:

- the workflow and recursive compilation state,
- effective target and root-input resolution,
- disabled-node propagation,
- executor routing validation,
- cache lookup and selected-record handling,
- attempt creation,
- arguments, templates, and contexts,
- task envelopes and future registries,
- deterministic result collection,
- DataFrame construction,
- canonical record publication,
- run/latest/output view updates,
- normalized progress and errors.

Parsl owns:

- the DataFlowKernel,
- executor operation,
- provider-based worker allocation,
- serialization and transport of app calls,
- execution of apps on selected executors.

Workers own:

- resolving one declared tool origin,
- creating or reusing a worker-local tool instance,
- running the requested `ProcessingTool` method,
- writing only within the supplied runtime paths or declared external destinations,
- returning a versioned result envelope.

BioImageFlow MUST NOT implement a second worker-block scheduler.
Site schedulers such as SLURM, PBS, LSF, and OAR are reached through Parsl providers for worker allocation.

---

## 5. Public API and Configuration

### 5.1 Workflow configuration

The workflow constructor accepts `"parsl"` as a serializable engine preference:

```python
Workflow(
    storage_path: str | Path,
    *,
    name: str = "workflow",
    display_name: str | None = None,
    engine: Literal["direct", "wetlands", "parsl"] = "wetlands",
    execution: Literal["parallel", "sequential"] = "parallel",
    on_progress: Callable[[ProgressEvent], None] | None = None,
    wetlands_config: dict[str, Any] | None = None,
    max_workers: int = 1,
    output_view: OutputView | Mapping[str, Any] | str | None = None,
) -> None
```

`parsl.Config`, a live DataFlowKernel, executor objects, callables, and environment secrets are runtime objects.
They MUST NOT be added to the strict schema-version-1 workflow graph or archive envelope.

The serialized graph continues to store the engine preference string in `config.engine`.
The strict graph and archive schemas remain otherwise unchanged.

### 5.2 Attached engine construction

The canonical attached cluster API is explicit engine injection:

```python
engine = ParslEngine(
    parsl_config=config,
    executor_bindings=bindings,
    resource_lifetime="engine",
)

with engine:
    result = workflow.compute(inputs=inputs, engine=engine)
```

The required constructor is:

```python
class ParslEngine:
    def __init__(
        self,
        *,
        parsl_config: Any | None = None,
        dfk: Any | None = None,
        executor_bindings: Mapping[str, ExecutorBinding],
        node_routes: Mapping[str, str] | None = None,
        environment_routes: Mapping[str, str] | None = None,
        shared_runtime_root: str | Path | None = None,
        execution: Literal["workflow", "parallel", "sequential"] = "workflow",
        storage_mode: Literal["shared_fs", "staged"] = "shared_fs",
        task_policy: ParslTaskPolicy | None = None,
        resource_lifetime: ResourceLifetime | str = "execution",
    ) -> None: ...
```

`parsl_config` and `dfk` are mutually exclusive.

Phase 1 requires one of them.
There is no implicit cluster configuration.

An injected `dfk` requires `resource_lifetime="external"` and remains caller-owned.
`resource_lifetime="external"` without an injected `dfk` is invalid.

The constructor stores and validates local configuration only.
It MUST NOT compile a workflow, touch cache state, start a DataFlowKernel, provision an environment, or submit a task.

`execution="workflow"` applies the executed root workflow's `execution` policy.
The other values explicitly override it and make engine-injection precedence unambiguous.

`shared_runtime_root` is normalized to an absolute path when supplied.
It is required when a reachable tool uses `archive_module` materialization and optional otherwise; it is runtime state, never graph or cache identity.

### 5.3 Workflow engine factory

`Workflow.create_engine()` is the public factory and MUST support the Parsl backend.

It MUST add these backend-specific keyword-only arguments without storing live objects in the workflow:

```python
workflow.create_engine(
    *,
    resource_lifetime="execution",
    env_manager=None,
    parsl_config=None,
    dfk=None,
    executor_bindings=None,
    parsl_node_routes=None,
    parsl_environment_routes=None,
    parsl_shared_runtime_root=None,
    parsl_execution="workflow",
    parsl_task_policy=None,
)
```

Rules:

- For `engine="direct"`, only the default `resource_lifetime="execution"` is accepted because the backend owns no persistent execution resource; `env_manager`, other lifetime values, and all Parsl arguments are rejected.
- For `engine="wetlands"`, `resource_lifetime` controls manager ownership, `env_manager` injects an existing manager, and Parsl arguments are rejected.
- For `engine="parsl"`, `env_manager` is rejected, `executor_bindings` is required, `resource_lifetime` controls owned Parsl resources, and the Parsl arguments are forwarded to `ParslEngine`.
- `Workflow.create_engine()` resolves the default `parsl_execution="workflow"` to the workflow's configured `execution` value; an explicitly constructed engine may retain `"workflow"` for reuse across workflows.
- Explicit `engine=` passed to `compute()` or `compute_steps()` remains highest precedence.
- Bare `Workflow(storage_path="./results", engine="parsl").compute(...)` without runtime arguments MUST fail before graph execution with a clear message showing how to construct and pass a `ParslEngine`.

### 5.4 Optional dependency behavior

Parsl MUST be an optional dependency of the orchestrator package.
The distribution extra is named `bioimageflow[parsl]`.

Importing `bioimageflow`, constructing non-Parsl workflows, serializing workflows, validating workflows, and calling `Workflow.plan()` MUST NOT import Parsl.
Importing the public `ParslEngine` and JSON-safe Parsl configuration value types also MUST NOT import the external Parsl package.
The external package is loaded lazily only when a Parsl operation requires it.

Selecting the Parsl backend without the extra installed MUST raise an error that names the missing dependency and installation extra.

`bioimageflow.__all__` exports `ParslEngine`, `ParslTaskError`, `ParslTaskPolicy`, `WorkerSlotCapacity`, `ExecutorCapabilities`, `WorkerEnvironmentAttestation`, `ExecutorBinding`, `ResourceLifetime`, `WorkflowExecutionContext`, and `WorkflowCancelledError`.

### 5.5 Runtime-only engine configuration

The following values are runtime-only and root-scoped:

- live `parsl.Config`,
- live DataFlowKernel,
- executor bindings and attestations,
- scoped-node and environment-identity routes,
- shared runtime root for archive source materialization,
- task packing and in-flight limits,
- launcher backend state,
- environment variables and credentials.

Nested workflow snapshots do not carry these values.
The root execution context applies them to every compiled internal node.

Submitted mode serializes a JSON-safe representation of the required runtime configuration in the run submission record, not in the workflow graph.

---

## 6. Engine Lifecycle and Ownership

### 6.1 Lifecycle values

`ResourceLifetime` defines the resource-ownership vocabulary for direct, Wetlands, and Parsl engines:

| Value | After `execute()` or a completed/closed `execute_steps()` generator | `engine.close()` | Ownership |
|---|---|---|---|
| `"execution"` | Clean up the engine-created DFK and executors. | Idempotent cleanup. | One execution. |
| `"engine"` | Retain the engine-created DFK for reuse. | Clean up the DFK and executors. | Engine session. |
| `"external"` | Leave the injected DFK untouched. | Leave the injected DFK untouched. | Caller. |

The only public parameter and enum names are `resource_lifetime` and `ResourceLifetime`.
Direct accepts only its no-resource default policy.
Wetlands applies the values to manager ownership.
Parsl applies them to DFK and executor ownership.
Shared lifecycle validation, active-resource tracking, and cleanup dispatch use backend-neutral hooks.

`ParslEngine.close()` MUST be idempotent.
The engine MUST support `with ParslEngine(...)`.
Executing a closed engine MUST raise `RuntimeError`.

Closing or exhausting `compute_steps()` MUST apply the configured execution cleanup policy.
Breaking out of the generator and closing it MUST also perform cleanup.

### 6.2 DataFlowKernel acquisition

The engine SHOULD bind Python apps to an explicit DataFlowKernel rather than mutating Parsl global-loader state.

When the engine owns the DFK, it creates the kernel from `parsl_config` at execution startup and calls the documented DFK cleanup operation according to lifecycle policy.

When the DFK is external, the engine registers only futures it submitted.
It MUST NOT call global Parsl cleanup, clear the active global kernel, shut down caller executors, or wait for unrelated tasks.

### 6.3 Thread safety

Parallel orchestration may call remote dispatch concurrently for independent ready `ProcessingTool` nodes.

The Parsl engine MUST synchronize:

- DFK initialization,
- app construction caches,
- submitted-future registries,
- cancellation state,
- per-node in-flight counters,
- close and execution cleanup.

The engine MUST NOT serialize branch execution merely because Parsl dispatch is used.

One `ParslEngine` instance permits exactly one active `execute()` or `execute_steps()` call.
An overlapping call MUST fail before it mutates run, cache, cancellation, or future state.
Independent engine instances MAY share one externally owned DFK, but each tracks only its own futures.

A successful execution returns only after all of its submitted futures are terminal.
After failure or cancellation, the engine requests cancellation and drains every future that it submitted before returning or becoming reusable; a running non-cancellable task may therefore delay completion.
The engine MUST retain the future-to-attempt registry throughout the drain and MUST NOT clean an attempt while a possible writer remains.

`close()` during an active execution requests cancellation, waits for that execution's drain without holding registry locks, and then applies the lifecycle policy.
After `close()` begins, no new execution or task submission is accepted.

---

## 7. Startup and Preflight

Startup occurs in six ordered stages.

### 7.1 Compile and statically validate

Before acquiring cluster resources, the engine MUST:

1. Use the canonical recursive compiler to find reachable real-tool nodes, workflow boundaries, scoped names, and completion dependencies.
2. Apply disabled-node propagation.
3. Validate environment-name conflicts across reachable `ProcessingTool` nodes.
4. Build canonical cache plans to identify known hits, known misses, and `PENDING_UPSTREAM` nodes.
5. Reject unsupported task-boundary `ImageShared` or runtime `SharedArray` use that is statically visible.
6. Resolve each remote tool to a complete worker tool origin.
7. Resolve an executor binding for every remote node.
8. Reject partial workflows, unresolved tool classes, and unsupported storage mode.

Static validation MUST happen before any processing app is submitted.
Because a `PENDING_UPSTREAM` result key cannot be decided before its upstream selection exists, startup MUST conservatively validate routing and compatibility for every reachable executable `ProcessingTool` that may miss.
It MUST NOT claim to know every cache miss before execution.
A workflow whose complete reachable processing path is already known cached may finish without acquiring a DFK.

### 7.2 Resolve environment and resource routes

For every selected executor, the engine MUST validate declared compatibility with:

- the normalized `EnvironmentSpec` identity,
- the required compatible `bioimageflow-core` version or API level,
- the tool origin loading mode,
- all non-zero `ResourceSpec` fields,
- shared-filesystem requirements.

Executor-label equality alone is not proof of compatibility.

### 7.3 Provision before worker startup

Phase 1 supports `preinstalled` executor environments only.

The host is responsible for configuring each Parsl executor so its worker process starts in the attested Python environment.

Future provisioners MUST return a concrete interpreter, activation command, container, or equivalent executor-startup configuration before worker blocks start.

`WetlandsEnvManager.get_or_create()` launches Wetlands workers and therefore MUST NOT be used as a Parsl provisioner.
No Wetlands module executor may run inside a Parsl app.

### 7.4 Materialize shared archive sources

Before probing tool origins, the orchestrator materializes each required archive custom-source bundle beneath `ParslEngine.shared_runtime_root`.
A missing root with any reachable `archive_module` origin fails static validation; the normalized root must be writable by the orchestrator and readable by every routed executor through the same absolute namespace.

Materialization MUST:

- validate the archive source ID and content hash before installation,
- stage into a unique temporary sibling and install atomically into a hash-addressed immutable directory,
- use a guard or equivalent create-if-absent protocol for concurrent reuse,
- reject symlink or path traversal outside the materialization root,
- retain a directory while any engine session may import it,
- clean only unreferenced transient staging directories under an explicit retention policy.

### 7.5 Acquire the DataFlowKernel

Only after static validation, provisioning, and source materialization succeed may the engine create or attach to the DFK.

### 7.6 Run executor preflight apps

Before processing tasks, Phase 1 MUST run at least one preflight app on every selected executor label.

The preflight MUST verify:

- the selected executor actually runs the probe,
- the expected `bioimageflow-core` API is importable,
- the canonical shared storage root is readable and writable through a unique attempt-shaped sentinel path that is not a real cache attempt,
- attempt and tool-source paths resolve within the expected shared namespace,
- every distinct tool origin assigned to that executor can be resolved without executing tool methods,
- package versions and archive source hashes match the declared origin where applicable.

The sentinel MUST be unique to the engine session and removed after verification.

Preflight failures occur before any processing task and name the executor, environment identity, tool origin, and failed capability.

---

## 8. Recursive Graph Compilation and Scheduling

### 8.1 WorkflowNode behavior

A `WorkflowNode` MUST never be sent to a remote worker as one task.

The engine consumes the canonical compiled graph:

1. Nested definitions are expanded recursively.
2. Real internal tool nodes receive scoped runtime names.
3. Incoming workflow field and DataFrame bindings are applied through stable interface IDs.
4. Enabled detached terminals are retained as completion dependencies.
5. Internal `ProcessingTool` nodes use normal Parsl dispatch.
6. Internal `DataFrameTool` nodes remain in the orchestrator.
7. The boundary DataFrame is assembled in the orchestrator from published outputs.
8. Published Series MUST have compatible indexes under the canonical alignment rules.
9. The assembled DataFrame uses public output names as column labels while stable output-port IDs preserve graph connectivity.
10. For a downstream binding that crosses the boundary, compilation preserves a recursive provenance recipe naming the real provider and declared selector without pretending that a runtime record is already selected.
11. After upstream finalization, the shared runtime identity resolver recursively substitutes the selected provider node key, result key, and record ID into that recipe.
12. A whole-DataFrame recipe additionally includes the stable output-port IDs and output-name mapping because renaming changes the assembled DataFrame labels.
13. Completion-only dependencies are excluded from downstream result-key material.
14. If any consumed published provider lacks a selected immutable record, the resolver returns no reusable result key and the downstream path uses the non-reusable execution path defined in Section 14.2.
15. The boundary may have a separate diagnostic signature, but it owns no result key, current pointer, or immutable record.

### 8.2 Parallel scheduling policy

Effective `execution="parallel"` supports branch-level concurrency:

- ready `DataFrameTool` nodes run under orchestrator coordination on the main thread,
- independent ready `ProcessingTool` nodes may dispatch concurrently,
- downstream nodes become ready only after all required upstream node results and completion gates are complete,
- workflow-boundary assembly remains local.

Effective `execution="sequential"` permits only one executable real-tool node at a time.
For `process_row`, it also limits BioImageFlow to one in-flight Parsl task at a time.

Phase 1 is node-finalization based.
It MUST NOT send a partial upstream node result to downstream nodes.

### 8.3 Disabled nodes

Disabled nodes follow the shared scheduler contract:

- disabled nodes perform no cache lookup and submit no task,
- downstream nodes that require a disabled node are skipped,
- disabling a workflow node disables its complete subtree,
- the enabled flag is not result-key material,
- multiple requested targets omit skipped targets when at least one target executes,
- all skipped targets raise `DisabledNodeError`.

### 8.4 Step execution

`compute_steps()` yields real tool steps only.
It does not yield `WorkflowNode` boundary steps.

Disabled ordinary real-tool steps are still yielded with `step.skipped is True`.
A disabled `WorkflowNode` is not expanded, so its internal subtree does not produce steps.
Calling `execute()` on a skipped step raises `DisabledNodeError`.

`NodeStep.prepare()` MUST delegate to an engine-neutral hook.

- The Wetlands engine may warm its environment.
- The Parsl engine may acquire the DFK, run the relevant preflight, or submit an explicit warmup if configured.
- Parsl preparation MUST NOT launch Wetlands workers.
- Cache hits require no worker warmup.

The shared engine layer MUST expose four backend-neutral seams:

1. a compiled-execution startup/preflight hook,
2. a `prepare_processing_node(...)` hook used by `NodeStep.prepare()`,
3. a processing dispatch hook receiving one immutable orchestrator request.
4. scheduler-level failure collection that retains independent-node failures until submitted work is drained and applies the canonical workflow-wide failure order.

The dispatch request contains the `ProcessingTool`, scoped node name, active workflow/run context, required invocation ID, optional reusable cache attempt ID, resolved argument dictionaries, ordered aligned indexes, row `ExecutionContext` objects, the batch context, and whether the tool overrides `process_batch`.
It returns the canonical `list[list[Outputs]]` shape or raises a normalized backend error.

The processing-dispatch seam is the only backend entry point for resolved processing work.
Direct, Wetlands, and Parsl MUST continue to share the surrounding argument resolution, optional result-key/cache-attempt, DataFrame construction, optional publication, progress, and run-view paths.

Advancing the generator auto-executes an unexecuted non-skipped step.

### 8.5 Planning

`Workflow.plan()` remains engine-independent and MUST NOT import Parsl, resolve a Parsl config, provision an environment, acquire a DFK, or run a probe.

It preserves:

- `CACHED`,
- `PRIOR_SELECTION_MISS`,
- `UNEXECUTED`,
- `SKIPPED`,
- `PENDING_UPSTREAM`,
- `pending_upstreams`,
- scoped internal tool entries,
- aggregate `WorkflowNode` entries,
- `CycleInWorkflowError` on cyclic graphs.

Aggregate workflow entries own no final result key or selected record ID.
Their status is:

- `SKIPPED` when the boundary is disabled or blocked by disabled input,
- `CACHED` only when every enabled executable internal real-tool node is cached,
- `PENDING_UPSTREAM` when any enabled internal node's final selected record is unknown,
- `UNEXECUTED` otherwise.

---

## 9. Processing Task Protocol

### 9.1 Parsl app construction

Wetlands and Parsl invoke the same top-level worker-safe function from `bioimageflow-core`.
Parsl wraps that function in a `PythonApp` bound to one explicit DFK and executor label.

The worker function imports its dependencies inside the worker process and accepts only picklable values.

Parsl app caching MUST be disabled.
BioImageFlow v1 caching remains the only result cache.

### 9.2 Versioned invocation envelope

Every task uses an explicit versioned envelope.

```python
@dataclass(frozen=True)
class ProcessingTaskV1:
    schema: Literal["bioimageflow.processing_task.v1"]
    task_id: str
    node_name: str
    invocation_id: str
    cache_attempt_id: Optional[str]
    task_retry: int
    mode: Literal["row_chunk", "process_batch"]
    tool: "WorkerToolOriginV1"
    rows: tuple[RowInvocationV1, ...]
    batch_context: Optional[dict[str, Any]] = None

@dataclass(frozen=True)
class RowInvocationV1:
    position: int
    row_index: str
    arguments: dict[str, Any]
    context: Optional[dict[str, Any]]
```

`invocation_id` identifies the processing-node execution and its reusable or transient workspace.
`cache_attempt_id` identifies the mutable reusable cache attempt and is `None` for non-reusable execution.
`task_id` identifies exactly one submitted row chunk or one whole-node batch task.
Task IDs use `task_` followed by sixteen lowercase hexadecimal digits encoding the zero-based submission sequence within the invocation.
The grammar is `^task_[0-9a-f]{16}$`; retries reuse the same task ID and increment only `task_retry`.
`task_retry` identifies a retry of one Parsl task envelope.
These identities MUST NOT be conflated.

The decoder requires the exact keys for the schema and mode.
It rejects unknown schemas, kinds, and modes; missing or extra keys; booleans where integers are required; invalid scalar types; duplicate positions; and malformed identifiers, paths, or hashes.
Invalid input fails before tool code runs.

### 9.3 Versioned result envelope

```python
@dataclass(frozen=True)
class ProcessingTaskResultV1:
    schema: Literal["bioimageflow.processing_result.v1"]
    task_id: str
    node_name: str
    invocation_id: str
    cache_attempt_id: Optional[str]
    task_retry: int
    mode: Literal["row_chunk", "process_batch"]
    rows: tuple[RowResultV1, ...]
    metrics: Optional[dict[str, Any]] = None

@dataclass(frozen=True)
class RowResultV1:
    position: int
    row_index: str
    outputs: tuple[dict[str, Any], ...]
```

The orchestrator MUST verify exact correspondence between the invocation and result:

- schema version,
- task ID,
- scoped node name,
- invocation ID,
- cache attempt ID,
- task retry number,
- mode,
- row positions,
- row indices.

Result decoding applies the same exact-key and scalar validation as invocation decoding.
Unknown, duplicate, missing, or unexpected positions fail the node before any output is accepted or published.

These worker-side examples use `Optional[...]`, not PEP 604 unions, because `bioimageflow-core` supports Python 3.9.

### 9.4 Worker instance identity

The worker resolves the complete tool origin and caches an instance by the canonical origin identity plus class name.

It MUST NOT cache solely by class name or canonical module name.

This prevents collisions between:

- two versions of one tool package,
- two archive source IDs containing equal class names,
- project modules with equal filenames,
- installed and development copies of one class.

### 9.5 Row execution

For `process_row` tools:

- a source node uses the canonical single input row with index `"0"`,
- each row task or chunk calls `process_row` exactly once per contained row,
- rows inside a chunk execute in increasing invocation position,
- a single `Outputs` object is normalized to one output,
- a list of `Outputs` preserves tool-return order,
- an empty output list emits no DataFrame row for that input,
- one row failure fails the whole task envelope.

The worker returns plain output dicts.
It does not construct pandas objects.

### 9.6 Batch execution

If a tool overrides `process_batch`, the complete node invocation is sent as one `process_batch` task in Phase 1.

The engine MUST NOT split `process_batch` unless a future tool contract explicitly declares partition safety.

The accepted return forms are:

- `list[list[Outputs]]` for zero, one, or many outputs per input row,
- `list[Outputs]` as the one-to-one shorthand.

The shared output-normalization helper MUST require a flat one-to-one result to match the invocation row count and a nested result to contain one group per invocation row.
One shared validator enforces this rule for direct, Wetlands, and Parsl.

### 9.7 Empty aligned batches and zero-row metadata

The Parsl engine implements the complete empty-input contract:

- An empty aligned batch does not call `process_batch` by default.
- `run_empty_batch=True` applies only to a tool class that overrides `process_batch`; without anchors it runs when the column-bound node's aligned index is empty, while anchored tools also run synthetic rows for partially empty parent groups.
- `empty_batch_anchor_inputs` is inspected in declared order; the first bound anchor with a usable non-empty DataFrame chooses synthetic indexes sorted by `str(index)`.
- Non-anchor inputs drive normal row alignment, and anchor indexes already represented by a normal row or descendant are not synthesized again.
- Other anchor bindings resolve by exact or parent-lineage matching against those chosen indexes.
- Without a usable anchor, the engine supplies the canonical single synthetic index `"0"`.
- Synthetic argument and template resolution happens in the orchestrator before dispatch.
- A resolved declared template file that exists is published even when the returned DataFrame has zero rows.
- `zero_row_scalar_outputs` produces manifest-only `scalar_output` entries for each synthetic execution row whose corresponding batch result group is empty.
- No sentinel DataFrame row is fabricated for an owned file or scalar metadata entry.

### 9.8 Shared output validation

Parsl MUST use the same output normalization and validation code as direct and Wetlands execution.

The orchestrator reconstructs or validates every returned plain dictionary against `tool.Outputs` before passing the canonical `list[list[Outputs]]` shape to shared DataFrame construction.
One orchestrator validator applies the checks below to direct, Wetlands, and Parsl.

At minimum the shared validator MUST:

- accept the documented row and batch return forms,
- reject missing and extra output fields,
- apply the library's lightweight annotation-based runtime checks,
- convert `Path` values to worker-safe strings for transport,
- reject undeclared fields,
- preserve declared output field order.

`IOModel` construction alone checks field presence but does not perform Pydantic type validation.
Pydantic remains orchestrator-only and MUST NOT be added to workers.

---

## 10. Task Packing, Backpressure, and Ordering

### 10.1 Task policy

```python
@dataclass(frozen=True)
class ParslTaskPolicy:
    row_chunk_size: int = 1
    max_in_flight: int = 32
```

Both values MUST be integers greater than or equal to one; booleans are rejected.

Phase 1 defaults to one row per task.
It MUST NOT silently choose a larger CPU chunk because chunking changes failure, cancellation, progress, scratch, and worker-state behavior.

An explicit `row_chunk_size > 1` groups consecutive aligned rows.

`max_in_flight` is a positive upper bound on submitted but unfinished futures for one node before `ResourceSpec.max_concurrent` and sequential policy are applied.

The effective per-node limit is:

1. one when `execution="sequential"`,
2. otherwise the minimum positive value of `ParslTaskPolicy.max_in_flight` and `ResourceSpec.max_concurrent`,
3. otherwise the task-policy limit.

The engine MUST use a sliding submission window.
It MUST NOT create an unbounded future list for a large DataFrame.

### 10.2 Deterministic collection

Task completion, progress callback, and DataFrame order are independent.

The orchestrator accumulates row results by integer `position` and verifies the original `row_index`.

Final DataFrame construction follows the canonical engine algorithm:

- iterate input positions in aligned-index order,
- preserve output order within each row,
- keep the parent index for exactly one output,
- use `parent::0`, `parent::1`, and so on for multiple outputs,
- emit no row for an empty output group,
- expose only declared `Outputs` columns,
- return an empty DataFrame with declared columns when the whole result is empty.

### 10.3 Failure selection

On the first observed task failure, the engine stops submitting new tasks and requests cancellation of unfinished submitted futures.

If several tasks fail before cancellation takes effect, the public primary failure MUST be deterministic.
Every submitted task receives a failure-order key consisting of the canonical compiled-node ordinal, the first input position (`-1` for a whole-node batch), and task ID.
Compiled-node ordinals come from deterministic topological order with scoped node path as the ready-node tie breaker.
After draining submitted futures, the smallest failure-order key among observed failures is primary across the whole workflow.

The shared scheduler retains sibling failures with their order keys, drains submitted work, and only then raises the deterministic primary.

Additional failures SHOULD be attached as structured diagnostics.

No accepted subset becomes a cache record.

---

## 11. Executor Routing and Environment Compatibility

### 11.1 Environment requirement identity

Each reachable `ProcessingTool` produces a worker requirement containing:

- `EnvironmentSpec.name`,
- the canonical normalized dependency hash,
- `allow_flexible_versions`,
- the compatible `bioimageflow-core` requirement or worker API level,
- any anchored local dependency references,
- `ResourceSpec`.

Same-name, different-dependency conflicts fail before DFK acquisition.

Local file or editable dependencies are not portable.
They require a verified shared path in Phase 1 and a staging/provisioning policy in a later phase.

### 11.2 Executor binding

```python
@dataclass(frozen=True)
class WorkerSlotCapacity:
    cpu: int
    gpu: int = 0
    memory_bytes: Optional[int] = None
    gpu_memory_bytes: Optional[int] = None

@dataclass(frozen=True)
class ExecutorCapabilities:
    storage_modes: tuple[Literal["shared_fs", "staged"], ...]
    tool_origin_modes: tuple[
        Literal[
            "installed_module",
            "versioned_module",
            "shared_module",
            "source_file",
            "archive_module",
        ],
        ...,
    ]
    slot: WorkerSlotCapacity

@dataclass(frozen=True)
class WorkerEnvironmentAttestation:
    name: str
    dependency_hash: str
    allow_flexible_versions: bool
    core_requirement: str

@dataclass(frozen=True)
class ExecutorBinding:
    label: str
    environments: tuple[WorkerEnvironmentAttestation, ...]
    capabilities: ExecutorCapabilities
```

An attestation is a deployment declaration and is verified by preflight where possible.

`executor_bindings` is keyed by executor label, and each mapping key MUST equal the contained binding's `label`.
Labels MUST be unique and MUST name executors present in the supplied config or DFK.

`node_routes` maps canonical scoped runtime node names to executor labels.
`environment_routes` maps canonical environment identities to executor labels.
The environment identity is derived by one shared helper from `EnvironmentSpec.name`, normalized dependencies, and `allow_flexible_versions`; it is not a display-name-only key.

The binding and capability objects MUST have strict versioned JSON-safe schema representations for submitted mode.
Unknown capability, origin, resource, or storage-mode values are rejected.

Environment and slot-capacity entries are trusted deployment attestations.
Preflight MUST verify interpreter, package, origin, and filesystem claims that it can observe, and SHOULD report observable CPU/GPU/memory mismatches, but it cannot prove scheduler allocation policy.
The deployment remains responsible for the truth of claims that cannot be probed safely.

One executor MAY attest several environment identities when its Python environment is a compatible superset.

Routing order is:

1. an explicit scoped-node route in the engine runtime configuration,
2. an explicit environment-identity route,
3. a unique compatible executor binding,
4. failure when zero or several bindings remain.

There is no implicit GPU, large-memory, default-executor, or environment-name fallback unless a declared binding makes compatibility unambiguous.

### 11.3 ResourceSpec

Phase 1 uses executor-label routing to homogeneous worker slots and does not pass `parsl_resource_specification`.
Each provider/executor configuration MUST give every task on a label at least the attested `WorkerSlotCapacity`.

- `cpu` must be a positive integer no greater than the attested slot CPU count.
- `gpu` must be a non-negative integer no greater than the attested slot GPU count.
- `memory` and `gpu_memory` use one canonical size parser and must not exceed the corresponding declared byte capacity.
- A requested non-zero or non-default value with no declared capacity is rejected.
- `max_concurrent` is enforced by the BioImageFlow submission window.

`parsl_resource_specification` is executor-specific, not a universal Parsl resource schema.
Support for Work Queue, TaskVine, or another per-task resource dictionary requires a later named, versioned built-in adapter with executor-specific tests; arbitrary adapter import strings are forbidden.

Requested resources MUST NOT be silently discarded.

### 11.4 Wetlands-specific workflow settings

`WorkflowEnvironment.max_workers`, `worker_env`, and `worker_timeout` are Wetlands settings.

They do not configure Parsl executor capacity or worker environment variables, and Phase 1 exposes no Parsl task-timeout policy.

Parsl uses:

- executor/provider configuration for worker count and environment,
- `ParslTaskPolicy.max_in_flight` and `ResourceSpec.max_concurrent` for BioImageFlow submission bounds.

The implementation MUST reject Wetlands-only per-environment fields when a Parsl engine is selected rather than reinterpreting them silently.

---

## 12. Worker Tool Origins

### 12.1 Origin variants

`WorkerToolOriginV1` is a discriminated worker-safe identity.

It is the following Python-3.9-compatible union in `bioimageflow-core`:

```python
@dataclass(frozen=True)
class InstalledModuleOriginV1:
    schema: Literal["bioimageflow.worker_tool_origin.v1"]
    kind: Literal["installed_module"]
    distribution: str
    version: str
    module: str
    class_name: str

@dataclass(frozen=True)
class VersionedModuleOriginV1:
    schema: Literal["bioimageflow.worker_tool_origin.v1"]
    kind: Literal["versioned_module"]
    distribution: str
    import_package: str
    version: str
    canonical_module: str
    scoped_module: str
    store_root: str
    class_name: str

@dataclass(frozen=True)
class SharedModuleOriginV1:
    schema: Literal["bioimageflow.worker_tool_origin.v1"]
    kind: Literal["shared_module"]
    module: str
    import_root: str
    source_hash: str
    class_name: str

@dataclass(frozen=True)
class SourceFileOriginV1:
    schema: Literal["bioimageflow.worker_tool_origin.v1"]
    kind: Literal["source_file"]
    path: str
    source_hash: str
    class_name: str

@dataclass(frozen=True)
class ArchiveModuleOriginV1:
    schema: Literal["bioimageflow.worker_tool_origin.v1"]
    kind: Literal["archive_module"]
    source_id: str
    source_hash: str
    canonical_module: str
    scoped_module: str
    materialization_root: str
    class_name: str

WorkerToolOriginV1 = Union[
    InstalledModuleOriginV1,
    VersionedModuleOriginV1,
    SharedModuleOriginV1,
    SourceFileOriginV1,
    ArchiveModuleOriginV1,
]
```

Decoding requires exact keys for the selected `kind`; unknown or extra keys are rejected.
Module, distribution, class, and hash strings are non-empty and use canonical normalized spellings.
Distribution identity and import-package identity are independent and MUST NOT be inferred from each other.
`import_package` is the explicit package imported from the versioned store.
Every filesystem string is absolute, normalized, preflight-verified, and confined to its declared shared root where applicable.

Canonical worker instance identity is the SHA-256 digest of the complete origin, including `class_name`, encoded as canonical JSON.
Logical cache tool identity continues to use the platform's tool/version/source-hash contract and does not acquire shared runtime paths.

### 12.2 Canonical worker loading

Wetlands and Parsl use one strict origin decoder, resolver, processing entry point, and worker-instance cache.
There is no tuple entry point, informal JSON token, source-file-only alternate path, permissive decoder, or alternate instance-cache key.

Installed-module mode MUST import from the worker's configured environment and verify the requested distribution version.
Missing or mismatched distribution metadata fails preflight; the deployment must use another origin variant when no distribution identity exists.

An absolute orchestrator `sys.path` MUST NOT be sent as an installed-module import root unless preflight proves it is a shared deployment path.

### 12.3 Archive custom sources

The workflow archive has one hash-validated `custom_sources` table shared across the recursive graph.

An orchestrator-local temporary extraction directory is not automatically visible to cluster workers.

In Phase 1, the engine or launcher MUST materialize required archive sources under a verified shared runtime directory before preflight, preserving the scoped archive loader and source hash.

If shared materialization is unavailable, the run fails before processing tasks.

Later no-shared-filesystem mode may transfer the verified bundle and materialize it worker-locally.

### 12.4 Import safety

Worker errors MUST report:

- executor label,
- origin variant,
- distribution and version when present,
- module and class,
- source ID and hash when present,
- source path or import root when present.

Package installation MUST NOT occur as a side effect of resolving a tool inside a compute task.

---

## 13. DataFrameTool Policy

Every `DataFrameTool` remains in the orchestrator in Phase 1 and Phase 1b.

The Parsl engine delegates to the canonical local path:

1. gather positional upstream DataFrames or root DataFrame inputs,
2. resolve constants and defaults under the normative runtime argument rules,
3. make path-typed arguments absolute,
4. compute canonical result-key material from selected upstream record references, recursively flattened published-provider references, declared selectors, and canonical logical digests of root DataFrame inputs,
5. use v1 current-record lookup only when the runtime resolver produced a reusable result key,
6. call `merge_dataframes`,
7. call `transform`,
8. publish through the canonical DataFrame cache path when reusable, otherwise return the in-memory non-reusable result without creating a record,
9. update progress and run/output views.

Source `DataFrameTool`s, `accepts_upstream=False`, `Passthrough`, dynamic output schemas, and tool-controlled output indexes follow the platform contract.

One shared helper applies the storage contract's canonical logical DataFrame rules to root DataFrame values, including index, schema, scalar kinds, and normalized external-path values.
Direct, Wetlands, Parsl, and submitted execution all use it.
Run-local transport paths and Parquet byte digests are transport integrity only and MUST NOT enter result-key material.

Remote `DataFrameTool` is a separate future proposal because it would change:

- the orchestrator-only package boundary,
- the two-stage merge/transform lifecycle,
- DataFrame transport and partition semantics,
- dynamic schema behavior,
- potential tool-facing execution context and asset APIs,
- the Python baseline of remote workers.

No `remote_execution` or partition-kind class attribute is added by Phase 1.

---

## 14. Cache, Storage, Paths, and Provenance

### 14.1 Normative storage authority

`docs/source/reference/output_cache_storage.md` is the exhaustive storage contract.

The Parsl engine MUST call the canonical `Storage` and cache publication APIs or shared refactored equivalents.
It MUST NOT introduce a second cache layout or select records by directory enumeration.

The logical layout is:

```text
<storage_path>/
  cache/v1/results/<result-shard>/<result-key>/
    attempts/<attempt-id>/staging/
      dataframe.parquet
      assets/
      work/rows/
      work/batch/
    records/<record-id>/
      manifest.json
      dataframe.parquet
      assets/
    current.json
    conflicts/
  cache/v1/transient/runs/<run-id>/nodes/<node-key>/<invocation-id>/
    invocation.json
    assets/
    work/
  views/runs/<run-id>/
  views/latest/
  outputs/runs/
  outputs/latest/
```

Callers and tests SHOULD use `Storage` helpers rather than assume the shard depth.

There is no mandatory `record.json` or `artifacts.json` sidecar.

### 14.2 Shared result-key identity

Parsl delegates result-key composition to the canonical library implementation.

Equivalent direct, Wetlands, and Parsl executions MUST produce the same result key when they consume the same selected upstream records or the same recursively flattened published-provider record references and selectors.

Completion-only workflow dependencies and workflow-boundary diagnostic signatures MUST NOT enter result-key material.
If any consumed boundary value cannot be traced to a selected immutable provider record, reusable lookup and publication are disabled for that downstream path as required by the storage contract.

The shared runtime cache-identity resolver returns `str | None` only after every consumed upstream selection is known.
Compilation preserves provenance recipes; it never embeds a fake `signature:<diagnostic>` token as a selected record substitute.

When the resolver returns `None`:

- the node performs no reusable lookup,
- it creates no result-key attempt, immutable record, `current.json`, latest pointer, run-node record pointer, or output projection,
- progress `started`/`completed` events carry no result key or record ID,
- it still executes and returns its DataFrame for in-run consumers,
- every downstream consumer of that value also remains non-reusable unless a later explicit materialization contract creates a real selected record.

An uncacheable `ProcessingTool` uses the canonical non-reusable run-scoped transient tree:

```text
<storage_path>/cache/v1/transient/runs/<run-id>/nodes/<node-key>/<invocation-id>/
  assets/
  work/
```

These assets are available to the current attached run and are never record-relative or reusable cache inputs.
They remain until explicit transient cleanup so paths returned to an attached caller do not disappear at method return.
`invocation.json` and optional `failed.json` are the only non-record diagnostics inside this tree.
The exhaustive storage contract defines identifier grammar, path confinement, explicit cleanup, and active-writer protection.
An implementation MUST NOT fabricate a result key to reuse the normal publication path.

The BioImageFlow run ID, invocation ID, engine type, executor label, provider, scheduler job ID, transfer mechanism, task ID, task retry, cache attempt ID, worker hostname, worker-local path, log path, launcher input/return path, and Parsl run directory MUST NOT enter result-key material.

Run IDs and all backend identifiers are also excluded from the content manifest used to derive the immutable record ID.

Resolved attempt output paths and `ExecutionContext` paths MUST NOT enter result-key material.

External-reference identity is the canonical normalized path-based identity defined by the storage contract.

### 14.3 Canonical DataFrame identity

The storage contract defines one canonical logical DataFrame digest and one deterministic Parquet transport.
Record manifests name `logical_digest`, `logical_schema`, and `transport_digest` explicitly.
The logical digest and schema enter immutable record identity; the Parquet byte digest is integrity metadata and does not.

Publication for `DataFrameTool`, `ProcessingTool`, and root DataFrame input identity uses the same logical digest helper.
Record validation byte-checks the transport digest, reloads the Parquet values, and recomputes the logical digest.
No reader, writer, fixture, or API accepts an ambiguous dataframe `digest` field.

### 14.4 Attempt and ExecutionContext paths

For shared-filesystem execution, `ExecutionContext` has this public shape:

- `run_dir`,
- `assets_dir`,
- `work_dir`,
- `rows_dir`,
- `row_dir`,
- `batch_dir`,
- `row_index`.

`context.run_dir` is the processing invocation workspace.
For reusable execution it is the node cache attempt's `staging/` directory and the task carries both invocation ID and cache attempt ID.
For non-reusable execution it is the transient invocation directory and the task carries no cache attempt ID.

All context paths and path-typed arguments are absolute before dispatch.

Every selected executor must observe the same absolute path namespace in Phase 1.
Equivalent but differently mounted paths require the later staging/path-mapping contract.

`row_dir` is private to one input row.
`batch_dir` is private to one whole-node batch call.
`work_dir` is shared by calls for the node, so tools that create shared runtime resources there are responsible for collision-safe atomic initialization.

Tools wrapping external programs use `cwd=context.row_dir` or `cwd=context.batch_dir`.
The engine and tools MUST NOT use process-wide `os.chdir()`.

### 14.5 Publication and path canonicalization

Workers write engine-owned resolved templated outputs beneath the invocation `assets/` directory and scratch beneath its `work/` directory.
For reusable execution the invocation root is attempt staging; for non-reusable execution it is the run-scoped transient root.
An explicitly declared external destination may be written outside staging and remains a declared external reference; it is never copied into a record implicitly.

The orchestrator publishes only after every required task result has been accepted and validated.

Publication MUST:

1. Build the complete output DataFrame in canonical input order.
2. Discover declared templated assets, including assets for zero returned rows.
3. Add declared `zero_row_scalar_outputs` to `manifest.outputs`.
4. Canonicalize owned DataFrame path cells to record-relative `assets/...` values.
5. Preserve legitimate declared external paths as external references.
6. Reject attempt work paths and undeclared worker-local paths.
7. Compute the canonical logical DataFrame schema and digest.
8. Write Parquet with the canonical transport settings and compute its byte digest.
9. Build and validate the record manifest, using the logical digest for record identity and the transport digest only for integrity.
10. Compute every file or canonical directory-tree asset digest and require an explicit asset type.
11. Install the immutable record.
12. Perform the guarded `first-valid` current-record selection.
13. Load and use the selected record, even when this attempt produced a different valid candidate.

At runtime, cache loading rehydrates record-relative asset values to absolute paths beneath the selected record.
For record-owned asset values, it MUST reject absolute persisted values, `..`, symlink escapes, and any resolved path outside that exact record directory.
Declared `external_path` values remain normalized absolute external references and are not subjected to record-containment checks.

Persisted DataFrames MUST NOT contain attempt, `work/`, worker-local, or human output-view paths.

Every owned manifest asset declares `asset_type="file"` or `asset_type="directory"`.
Directory identity is the canonical sorted tree defined by the storage contract.
Symlinks, special files, traversal, duplicate normalized paths, and case-normalization collisions fail publication or record validation.
Cache-hit rehydration and run/output-view materialization preserve the validated directory root; direct, Wetlands, and Parsl use this same path.

### 14.6 Guarded first-valid selection

Atomic replacement of `current.json` prevents torn JSON but is not sufficient for concurrent selection.

Reading current state, deciding the outcome, and creating `current.json` or a conflict report MUST occur under a per-result-key guard such as atomic create-if-absent, a safe directory lock, filesystem locking, or backend compare-and-swap.

If a valid current record already exists:

- the existing record remains selected,
- an equivalent candidate is a duplicate success,
- a different candidate is a conflict,
- downstream execution and the current run use the selected existing record.

A corrupt current pointer is a cache-corruption error and MUST NOT be silently replaced.

### 14.7 Invocation cleanup

A failed, cancelled, or abandoned reusable attempt is never selected.
A failed, cancelled, or abandoned transient invocation never creates a record or view pointer.

The engine MUST NOT delete an attempt or transient invocation directory while a remote task may still write into it.

An attempt or transient invocation may be removed only after every associated writer is known to be terminal.
Otherwise it remains unselected and is eligible for coordinated transient cleanup under the canonical storage policy.

Published record pruning is never an automatic engine cleanup action.

### 14.8 Run and output views

Parsl execution uses the same run-view hooks as direct and Wetlands execution.

For every successfully selected real-tool record, the engine updates:

- `views/runs/<run-id>/nodes/<scoped-node>/result.json`,
- the record pointer and owned-output pointers,
- `views/latest/<scoped-node>.bioimageflow-link.json`,
- optional configured `outputs/` projections.

The latest successful run pointer is updated only when the whole requested execution succeeds.

Automatic output-view materialization failures remain warnings and do not turn successful computation into failure.
Explicit output export remains strict.

Run metadata MUST record the effective engine identifier `"parsl"` and scheduling policy, not merely the workflow's stored engine preference.

The active workflow run ID MUST be passed into non-content publication provenance such as attempt diagnostics, `current.json.selected_by`, and run views.
It MUST NOT enter result-key material, the record content manifest, or record-ID hashing.
Publication MUST NOT invent an unrelated attempt-derived run ID when a workflow run is active.

---

## 15. Shared Memory

POSIX shared-memory names are host-local and cannot safely cross a general Parsl task boundary.

Phase 1 MUST reject:

- a remote task input whose runtime value is `SharedArray`,
- a remote task output whose runtime value is `SharedArray`,
- an `ImageShared` input that would be supplied to a remote `ProcessingTool`,
- an `ImageShared` output that a remote `ProcessingTool` would produce.

The error recommends file-based `Annotated[Path, ImageSpec(...)]` fields.

The rejection is task-boundary based, not a claim that the rest of BioImageFlow lacks shared-memory caching.
Local engines continue to publish reusable shared arrays as record-owned `.npy` assets and rehydrate fresh host-local segments.

A fully cached result may be returned without a Parsl task when no `SharedArray` value is subsequently sent to a remote task.
The engine MUST avoid rehydrating a shared-memory cache value merely to discover that it cannot be dispatched; static schema preflight should reject that downstream route first.

Submitted-run return persistence MUST materialize or reject any remaining `SharedArray`; it cannot persist a process-local name.

---

## 16. Progress, Logging, Errors, Retries, and Cancellation

### 16.1 Progress

The public `ProgressEvent.status` vocabulary remains:

- `started`,
- `row_progress`,
- `row_complete`,
- `completed`,
- `cached`,
- `failed`,
- `cancelled`.

Scoped recursive node names are used in events.

The fields are `node_name`, `status`, `row`, `total_rows`, `message`, `current`, `maximum`, `timestamp`, `result_key`, and `record_id`, with the meanings defined by the platform progress contract.
`row` is a zero-based aligned position, not the DataFrame index label.

`started` SHOULD include a known result key.
`completed` and `cached` MUST include the selected record ID when the node owns a cache record.

`row_complete` means the orchestrator accepted that aligned position into the node accumulator.
For row and row-chunk execution, events are buffered and emitted once per row in increasing aligned position, even when futures finish out of order; when a chunk closes a gap, its newly contiguous rows emit in order.
Whole-node `process_batch` emits no `row_complete` events.

Parsl queued/running state, executor labels, task IDs, scheduler job IDs, and retry numbers are backend metadata.
They MUST NOT become new `ProgressEvent.status` values without changing the platform progress contract.

Progress callbacks remain serialized through the engine's callback lock.
Independent nodes may interleave events.

Phase 1 does not provide tool-level `task.update()` progress inside Parsl workers.

### 16.2 Logging

The engine and launcher MUST NOT install host console handlers implicitly.

Parsl framework logs, orchestrator stdout/stderr, and optional task diagnostic logs live outside immutable cache records and result-key material.

Python-app stdout/stderr is not assumed to be captured automatically by every Parsl executor.
If Phase 1 exposes task stdout/stderr, the worker wrapper must redirect it explicitly to unique diagnostic files and return log references.

Log records SHOULD include:

- run ID,
- scoped node name,
- invocation ID,
- cache attempt ID when reusable,
- task ID,
- task retry,
- executor label,
- row position or range,
- worker hostname and process ID when available.

Logs are not required to reconstruct results.

### 16.3 Errors

The engine MUST expose a structured remote-task error that includes:

- scoped node name,
- tool origin,
- executor label,
- task ID,
- invocation ID,
- cache attempt ID when reusable,
- task retry,
- row position or range,
- original exception type and message,
- remote traceback when Parsl provides it.

The backend raises a public `ParslTaskError` subclass of `WorkerTaskError` and exports it from `bioimageflow`.

The following fail before processing submission when detectable:

- missing optional dependency,
- missing Parsl configuration or DFK,
- unsupported storage mode,
- ambiguous or missing executor route,
- unattested environment identity,
- incompatible core API,
- unsupported resource request,
- same-name environment conflict,
- unresolved tool origin,
- failed shared-filesystem or import preflight,
- unsupported shared-memory task boundary,
- partial or invalid workflow.

### 16.4 Retries

Phase 1 exposes no retry-count setting.
The supplied Parsl `Config.retries` MUST equal zero, and the engine MUST reject executor behavior that performs opaque retries which cannot be correlated with task envelopes and output paths.

`task_retry` remains fixed at zero in Phase 1 task envelopes so a future version can add visible retries without conflating them with invocation or cache attempt IDs.

Future retries MUST:

- resubmit the same logical row positions,
- increment `task_retry`,
- remain inside the same invocation,
- remain inside the same cache attempt when the invocation is reusable, or explicitly define a new reusable-attempt policy,
- use retry-safe scratch paths,
- expose retry diagnostics,
- ignore stale completions from older retries,
- retry a failed chunk as the same chunk unless an explicit split policy is configured.

Silent chunk-to-row fallback is not allowed.

### 16.5 Cancellation

Cancellation is best-effort because a running Parsl task may not be cancellable.

The shared execution layer adds the runtime-only public type `WorkflowExecutionContext` and keyword-only `run_context` parameter:

```python
class WorkflowExecutionContext:
    run_id: Optional[str]
    defer_success_finalization: bool

    @property
    def cancel_requested(self) -> bool: ...

    def request_cancel(self) -> None: ...
    def finalize_success(self) -> None: ...
    def finalize_failure(self, error: BaseException) -> None: ...

workflow.compute(..., engine=engine, run_context=run_context)
workflow.compute_steps(..., engine=engine, run_context=run_context)
```

`WorkflowExecutionContext` owns a validated optional preallocated run ID, one thread-safe cancellation token, and an idempotent terminal-finalization guard.
When omitted, the public call creates a fresh context and run ID and finalizes it normally.
`defer_success_finalization=True` is reserved for the submitted launcher: successful compute leaves the canonical run non-terminal until the launcher persists the return and calls `finalize_success()`; persistence failure calls `finalize_failure()`.
Execution failure or cancellation finalizes the canonical run immediately even when success finalization is deferred.
Calling either finalizer in the wrong state or with a different execution binding raises `RuntimeError`.
The synthetic parent used by root `compute(inputs=...)` and `compute_steps(inputs=...)` MUST receive the same context, and `Workflow.cancel()` signals the currently active context rather than a definition-local flag.

One `Workflow` object supports one active public execution at a time.
Both compute APIs detach any prior finished context at entry; an internally created context starts clear, while a supplied launcher context preserves a cancellation request made before orchestrator startup.

Required behavior:

1. `compute()` and `compute_steps()` establish the fresh or explicitly supplied active execution context before compilation.
2. `Workflow.cancel()` sets the orchestrator cancellation flag.
3. The engine stops submitting new tasks.
4. It calls `cancel()` on submitted unfinished futures and records whether cancellation was accepted.
5. Running tasks may continue according to executor behavior and are drained before the engine returns.
6. Results received after cancellation are ignored for result construction and publication.
7. The incomplete node attempt is never published or selected.
8. Attempts remain retained until every associated future is terminal and are then eligible for transient cleanup.
9. Records selected by nodes completed before cancellation remain valid and are not rolled back.
10. A running `DataFrameTool` call is not interruptible; cancellation is observed after it returns or raises.
11. The engine emits normalized `cancelled` progress and raises `WorkflowCancelledError`.
12. Cleanup affects only engine-owned Parsl resources.

Calling `cancel()` while idle remains a no-op for later executions because no active context receives the request.

---

## 17. Submitted Launcher and WorkflowRun

### 17.1 Layering

`ParslEngine.execute()` runs in the current process.
It MUST NOT secretly submit a new orchestrator job.

The launcher is a separate layer above workflow execution.
CLI and GUI MUST use the same launcher API.

### 17.2 Launch API

```python
@dataclass(frozen=True)
class ParslConfigRef:
    factory: str
    kwargs: dict[str, Any]
    secret_refs: Optional[dict[str, str]] = None

@dataclass(frozen=True)
class OrchestratorLaunchConfig:
    backend: Literal["local", "manual"] = "local"
    work_dir: Optional[Path] = None
    hard_cancel_after: Optional[float] = None

@dataclass(frozen=True)
class PSIJLaunchConfig:
    executor: Literal["slurm", "pbs", "lsf"]
    walltime: timedelta
    queue: Optional[str] = None
    project: Optional[str] = None
    cpu_cores: int = 1
    work_dir: Optional[PurePosixPath] = None
    hard_cancel_after: Optional[float] = None

def submit_workflow(
    workflow: Workflow,
    *,
    inputs: Mapping[str, Any] | None = None,
    targets: Sequence[str] | None = None,
    parsl_config: ParslConfigRef,
    executor_bindings: Mapping[str, ExecutorBinding],
    node_routes: Mapping[str, str] | None = None,
    environment_routes: Mapping[str, str] | None = None,
    shared_runtime_root: Path | None = None,
    task_policy: ParslTaskPolicy | None = None,
    launch: OrchestratorLaunchConfig | PSIJLaunchConfig | None = None,
) -> WorkflowRun: ...
```

`factory` is an importable `module:callable` reference returning a Parsl config from JSON-safe arguments.
The host SHOULD resolve it through a trusted allowlist.
Arbitrary pickle is forbidden.

`kwargs` MUST NOT contain credentials or literal secrets.
`secret_refs` contains opaque names resolved by the host's configured secret provider at orchestrator startup; only the names are persisted.
Manual mode MUST fail if its host cannot resolve every required reference without embedding a value in the launch command or files.

`hard_cancel_after`, when set, is a positive grace period in seconds before the launcher may terminate an unresponsive orchestrator after a cancellation request.
It is disabled by default because graceful orchestrator cancellation is required for Parsl future draining and DFK cleanup.

`PSIJLaunchConfig` is the scheduler launch configuration.
Its executor is exactly `slurm`, `pbs`, or `lsf`; OAR is not supported.
Its walltime is an explicit positive `timedelta`, its core count is a positive integer, and its optional queue, project/account, cluster working directory, and hard-cancel grace use strict JSON-safe codecs.
Queue and project/account values are scheduler identifiers, not native directive or shell-fragment escape hatches.
The cluster working directory is a normalized absolute POSIX path.
Live PSI/J jobs, executors, configuration objects, scripts, native attributes, environment mappings, and literal secrets are not accepted.

Live DFKs, live executor objects, open files, and non-importable callables are attached-mode values and are rejected by submitted mode.
Submitted mode normalizes and persists `shared_runtime_root` as an absolute path and applies the same archive-origin requirement and preflight as attached mode.

### 17.3 Invocation variants

The submission record uses exactly one invocation variant.
`inputs` and `targets` are mutually exclusive; supplying both is an error.

**Root interface invocation**:

- used when `targets` is omitted, with `inputs=None` treated as an empty input mapping,
- executes `workflow.compute(inputs=...)`,
- records inputs by stable interface port ID and current name,
- records the stable published output-port IDs expected from the definition,
- runs detached completion branches,
- persists the single public boundary DataFrame, including the zero-output empty DataFrame.

**Ad hoc graph invocation**:

- used when `targets` is supplied and `inputs` is omitted,
- identifies registered immediate target nodes by structural name,
- rejects unknown or nested internal names as public targets,
- executes the same single-target or multi-target return contract as `Workflow.compute(*targets)`.

Raw unscoped target names are never used to address compiled internal nodes.

### 17.4 Input serialization

Scalar and path-like root inputs use the canonical constant serializer.
Path-like root values are resolved to normalized absolute paths at submission time so a different orchestrator working directory cannot change their meaning.

Root DataFrame inputs are externalized under the launcher control directory as Parquet with:

- stable input-port ID,
- schema and index metadata,
- the canonical logical DataFrame digest used by attached execution,
- a separate SHA-256 digest of the Parquet transport file,
- a confined relative control-directory path.

The orchestrator validates the relative path and transport digest, loads the frame, recomputes the canonical logical digest, and requires an exact match before DFK acquisition.
The run ID, relative transport path, and Parquet byte digest never enter result-key material.

Literal environment secrets MUST NOT be copied into a submission record, status, progress, command, or log.

### 17.5 Strict workflow payload

The submitted workflow payload is exactly one of:

- the schema-version-1 recursive graph from `Workflow.to_dict()`, or
- the archive-version-1 envelope from `Workflow.to_dict(include_custom_tools=True)`.

The archive envelope is the default when custom sources exist.

Launcher, Parsl, routing, and input state live beside the payload in submission metadata.
They MUST NOT be inserted as unknown graph fields.

Submitted execution rejects `workflow.is_partial`, unresolved `failed_nodes`, invalid archives, and validation errors before task submission.

Phase 1b requires the orchestrator package and worker environments to be preinstalled.
No package auto-install field or side effect is part of the launcher protocol.

### 17.6 Unified run identity

The launcher allocates the BioImageFlow run ID before starting the orchestrator.

Run IDs use the path-safe UUID4 format required by the storage contract.
Allocation atomically claims a new control directory and rejects a collision with either the launcher control path or canonical run-view path.

The launcher constructs `WorkflowExecutionContext(run_id=...)` and passes it through the public `run_context` argument.
Private workflow attribute mutation is forbidden.

The canonical workflow run view remains:

```text
<storage_path>/views/runs/<run-id>/
```

It contains only the portable `run.json`, node result JSON, and pointer files defined by the storage contract.
The library run metadata records the effective engine supplied at execution time.

Phase 1b uses a separate launcher control directory with the same run ID:

```text
<storage_path>/launcher/v1/
  .allocation.guard              # storage-wide run-ID allocation lock
  runs/<run-id>/
    .control.guard               # per-run state/progress lock
    submission.json              # immutable launcher request
    status.json                  # launcher/orchestrator state
    progress.jsonl               # append-only normalized and backend events
    execution.claim              # guarded execution/recovery lease after startup
    claims/                      # append-only superseded claim epochs
    cancel_requested             # optional marker
    error.json                   # terminal structured failure/lost error when applicable
    command.json                 # manual-mode command descriptor when applicable
    local_process.json           # reconnectable local process identity when applicable
    local_process_exit.json      # observed local process exit when applicable
    psij_intent.json              # immutable scheduler submit intent when applicable
    psij_job.json                 # immutable native-job receipt when available
    psij/executor/                # fixed shared PSI/J executor work directory
    logs/orchestrator.out
    logs/orchestrator.err
    inputs/
    return/manifest.json
    return/dataframes/
    return/assets/
```

Canonical node and latest views remain the public provenance views.
The launcher metadata records the confined relative canonical view path.
The launcher MUST NOT put logs, Parquet inputs, return DataFrames, mutable status, or cancellation markers under `views/`.
It MUST NOT create a second result cache or a second run identity.

`docs/source/reference/output_cache_storage.md` defines the normative launcher schemas, confinement rules, and retention behavior.

Required control artifacts are immutable `submission.json`, mutable guarded `status.json`, and append-only `progress.jsonl`.
`execution.claim` is required once startup is claimed; input files, return files, manual command, error, cancellation marker, local-process identity and exit observations, and logs are conditional on their corresponding modes and states.

Every persisted relative path is normalized and confined beneath either the control directory or the explicitly named canonical run view.
Readers reject absolute paths, empty or dot segments, `..`, symlink escapes, and targets outside those roots.

Submission metadata, inputs, terminal status/error, and successful returns remain durable until explicit run deletion.
Prepared/manual and failed runs are never removed automatically merely because they are old.
Logs and progress may be pruned only by an explicit, documented retention operation that preserves status, error, return, and provenance metadata; temporary sibling staging directories may be cleaned after their writer is known dead.

### 17.7 Run state machine

The control protocol has these exact schema names:

- `bioimageflow.launcher.submission.v1` for `submission.json`,
- `bioimageflow.launcher.status.v1` for `status.json`,
- `bioimageflow.launcher.claim.v1` for `execution.claim`,
- `bioimageflow.launcher.progress.v1` for every `progress.jsonl` entry,
- `bioimageflow.launcher.error.v1` for `error.json`,
- `bioimageflow.launcher.command.v1` for `command.json`,
- `bioimageflow.launcher.local_process.v1` for `local_process.json`,
- `bioimageflow.launcher.local_process_exit.v1` for `local_process_exit.json`,
- `bioimageflow.launcher.psij_intent.v1` for `psij_intent.json`,
- `bioimageflow.launcher.psij_job.v1` for `psij_job.json`,
- `bioimageflow.launcher.return.v1` for `return/manifest.json`.

The immutable submission records the run ID, creation time, canonical storage root, confined canonical-view path, normalized shared runtime root when present, workflow payload kind/digest and payload, invocation variant, serialized inputs, Parsl config reference, bindings, routes, task policy, launch backend, and protocol versions.
The canonical storage root is launcher runtime metadata, not workflow definition data: it remains absent from the recursive graph and portable archive, and the orchestrator passes it explicitly to `Workflow.from_dict(..., storage_path=...)`.
The status records the run ID, exact state, monotonically increasing revision, created/updated timestamps, backend identifier, orchestrator identity, cancellation and hard-termination flags, and terminal error reference when present.
Each progress entry records schema, run ID, globally monotonic sequence, timestamp, `public` or `backend` kind, and a versioned payload.
The error records a stable public error code, exception type/message, optional traceback, and available node/task/backend metadata without secret values.

The exact state set is:

- `prepared`,
- `starting`,
- `running`,
- `finalizing`,
- `cancel_requested`,
- `succeeded`,
- `failed`,
- `cancelled`,
- `lost`.

Legal transitions are:

| From | To |
|---|---|
| `prepared` | `starting`, `cancelled`, `failed` |
| `starting` | `running`, `cancel_requested`, `failed`, `lost` |
| `running` | `cancel_requested`, `finalizing`, `failed`, `lost` |
| `finalizing` | `succeeded`, `failed`, `lost` |
| `cancel_requested` | `cancelled`, `failed`, `lost` |
| `succeeded`, `failed`, `cancelled`, `lost` | none |

Terminal states and their terminal metadata are immutable.
`lost` means a claimed backend process or job disappeared without a valid terminal outcome; an unstarted manual run remains `prepared`, not `lost`.

The initial control-directory claim creates `submission.json`, revision-zero `status.json`, and `progress.jsonl` before returning `WorkflowRun`.
Under one control-directory guard, an orchestrator creates or takes a lease on `execution.claim` and compare-and-swap transitions the status revision from `prepared` to `starting` before it may execute any workflow code.
The claim records owner identity, backend identity, random nonce, monotonically increasing epoch, heartbeat, and expiry.

A live unexpired claim excludes another orchestrator.
If a process dies after writing the claim but before committing `starting`, a controller may reclaim only after the lease expires and the backend is confirmed absent; this is safe because the protocol forbids execution before `starting` commits.
After `starting`, a takeover MUST NOT rerun the workflow: an authorized recovery controller may only complete an already installed finalization or transition the run to `lost`.
Superseded claim epochs remain in append-only claim history for diagnosis.

All status mutation is serialized by the control-directory guard rather than assigned to one process forever.
The submitter may cancel or fail an unclaimed prepared run, the claimed orchestrator owns ordinary execution/finalization transitions, a client may request cancellation, and an authorized backend monitor/recovery controller may record loss or complete an idempotent finalization after claim expiry.
Every actor validates its allowed transition, expected revision, and claim epoch, writes a same-directory temporary file, and atomically replaces `status.json`.
Any authorized writer that emits progress allocates the next sequence under the same guard; reconnecting readers never allocate sequences.

Manual mode writes the complete submission and a structured reproducible command descriptor, then remains `prepared` until an external actor claims startup.

`status.json` is authoritative for launcher reconnection and `WorkflowRun` behavior.
Canonical `views/runs/<run-id>/run.json` remains authoritative for cache/run provenance, and `views/runs/latest-success.bioimageflow-link.json` changes only after launcher return persistence succeeds.
The two are mapped as follows:

- launcher `running` corresponds to a non-terminal canonical run,
- launcher `finalizing` means a complete return is installed and canonical success is being committed,
- launcher `succeeded`, `failed`, and `cancelled` require the matching canonical terminal state whenever canonical workflow startup occurred,
- launcher `lost` leaves no successful canonical run and finalizes any started canonical run as failed with a stable orchestrator-lost code when recovery can safely do so,
- a run cancelled, failed, or lost before canonical workflow startup has no canonical run view.

Submitted success uses deferred canonical success finalization in `WorkflowExecutionContext`:

1. execute all nodes and write their canonical node views without marking the workflow run successful,
2. stage, validate, and atomically install the complete public return,
3. under the control guard, compare-and-swap `running` to the non-cancellable `finalizing` state,
4. finalize canonical `run.json` as succeeded and update latest-success,
5. atomically transition launcher status from `finalizing` to `succeeded`.

Return-persistence failure finalizes both scopes as failed and never updates latest-success.
Recovery after a crash between steps inspects the immutable return manifest, claim epoch, and canonical run state: a valid installed return in `finalizing` permits idempotent completion of steps 4 and 5; otherwise a disappeared backend becomes `lost` and cannot expose a result.

Cancellation is linearized by a guarded status transition.
An unclaimed `prepared` run transitions directly to `cancelled` and creates no canonical run view.
A `starting` or `running` run transitions to `cancel_requested`; `status.json` is the durable source of truth and `cancel_requested` is only a best-effort wake-up marker, so the orchestrator polls both.
If `cancel_requested` commits before the success claim, `running -> finalizing` fails and the run cannot succeed.
Cancellation of `finalizing` or a terminal state is a no-op because the success claim already won.

### 17.8 Persisted public return value

Every successful submitted run persists the exact public return shape under the launcher control directory's `return/` tree.

The return manifest records:

- schema name/version and run ID,
- `single` versus `mapping` shape and ordered mapping keys,
- a stable frame ID, confined relative Parquet path, canonical logical digest, transport digest, schema, and index metadata for every DataFrame,
- stable output-port-ID to current-name mapping for root interface execution,
- the zero-output boundary case,
- typed path-cell provenance.

Every record-backed path cell has a mandatory locator containing frame ID, optional mapping key, row position, row index, column name, provider result key, provider record ID, and record-relative asset path.
Declared external-path cells use a separate external-reference entry and never masquerade as record assets.
Every run-transient owned path cell is copied into a confined content-digested `return/assets/` path and uses a self-contained `return_asset` locator; no transient path is persisted.
Locators are derived from declared path schemas and compiler/provider provenance, never by guessing from string prefixes.

This snapshot is required because a `WorkflowNode` boundary owns no cache record.

Return persistence MUST canonicalize record-backed paths so `WorkflowRun.result()` can rehydrate them from the recorded immutable record without consulting the current pointer.
It MUST NOT persist attempt, scratch, temporary extraction, or shared-memory names.

The writer stages the complete return in a unique sibling directory, fsyncs where supported, validates every frame and locator, and atomically installs the directory before canonical or launcher success is committed.
`manifest.json` is the commit marker and no reader accepts a partial tree.

Historical result loading validates each frame digest and every exact referenced immutable record manifest directly.
For confined snapshot paths and record-owned asset locators, it rejects absolute or escaping paths, symlink escapes, missing manifest entries, digest mismatches, and asset paths outside the named record, and it never consults `current.json`.
Typed declared external references remain normalized absolute values and are validated as external references rather than record-owned paths.

Explicit record pruning remains allowed by the storage contract.
If a succeeded run references a record that was later pruned and no self-contained return value can reconstruct the cell, `WorkflowRun.result()` raises `WorkflowRunResultUnavailableError` naming the missing record rather than returning a stale path.

### 17.9 WorkflowRun

```python
class WorkflowRun:
    id: str
    control_dir: Path
    view_dir: Path
    status: str

    @classmethod
    def open(cls, storage_path: Path | str, run_id: str) -> "WorkflowRun": ...

    def refresh(self) -> None: ...
    def progress(self, *, after_sequence: int = 0) -> list[dict[str, Any]]: ...
    def logs(self) -> str: ...
    def cancel(self) -> None: ...
    def result(self) -> Any: ...
```

`result()`:

- returns the same single DataFrame or mapping shape as the submitted call,
- succeeds only for a `succeeded` run,
- raises `WorkflowRunFailedError` carrying the persisted structured error for `failed`,
- raises `WorkflowCancelledError` for `cancelled`,
- raises `WorkflowRunLostError` for `lost`,
- raises `WorkflowRunNotReadyError` for a non-terminal run,
- raises `WorkflowRunResultUnavailableError` for a succeeded run whose required immutable record was explicitly pruned or corrupted,
- loads recorded immutable records or return snapshots without relying on current cache selection.

`open()` validates the run ID, control path, immutable submission, and current status schemas and supports reconnection from only the storage path and run ID.

`cancel()` follows the state-specific guarded transitions above: it directly cancels an unclaimed prepared run, requests cancellation for `starting`/`running`, and is a no-op for `finalizing` or terminal state.
After a committed `cancel_requested` status, it best-effort creates the wake-up marker.
The orchestrator treats status as authoritative, also watches the marker for low-latency wake-up, and calls `Workflow.cancel()`.
It does not immediately terminate a live backend process.
Only after configured `hard_cancel_after` expires may the launcher request backend termination.
Killing an orchestrator does not prove that Parsl providers, workers, futures, or writers stopped, so a hard-terminated Phase 1b run becomes `lost`, retains possibly writable attempts, and never claims normal drain or cleanup.

`WorkflowRunFailedError`, `WorkflowRunLostError`, `WorkflowRunNotReadyError`, and `WorkflowRunResultUnavailableError` are public exports with stable error codes.

### 17.10 Launcher backends

The launcher has three exact backend discriminators:

- `local`: start a separate local orchestrator process,
- `manual`: write state and a reproducible command for external submission,
- `psij`: submit exactly one scheduler job for the orchestrator through PSI/J.

`OrchestratorLaunchConfig` accepts only `local` and `manual`.
Direct `slurm`, `pbs`, `lsf`, and `oar` backend aliases do not exist.
`PSIJLaunchConfig` selects a `slurm`, `pbs`, or `lsf` PSI/J executor.
Selecting PSI/J without the `bioimageflow[psij]` extra or without the requested executor descriptor raises `BackendNotSupportedError` before submit intent or external action.

The PSI/J adapter builds exactly one single-process `JobSpec` from the existing shell-free orchestrator argv.
It requests one node, one process, one process per node, the configured cores per process, and an explicit positive walltime.
It maps `queue` to `JobAttributes.queue_name` and `project` to `JobAttributes.account`.
It directs standard output and error to the existing confined launcher logs.
The job executable is the absolute cluster Python executable for the installed BioImageFlow environment, and the arguments contain only the orchestrator module, absolute storage root, and run ID.
The optional job working directory is the configured normalized absolute cluster path and MUST exist as a non-symlink directory when the initial submission begins.
Pre-launch scripts, post-launch scripts, native scheduler directives, custom attributes, arbitrary environment values, relative executables, and shell evaluation are forbidden.

The PSI/J `JobExecutor` uses a fixed shared work directory at `psij/executor/` beneath the run control directory.
Submission, reattachment, observation, and cancellation reconstruct the requested executor with that same directory.
The adapter verifies that the requested descriptor exists before creating submit intent.

Immediately before the external `submit()` action, the launcher atomically installs immutable `psij_intent.json`.
The intent records its exact schema, run ID, random submit token, creation time, executor name, fixed executor work directory, and the complete safe semantic job description.
Immediately after successful submit returns a valid native job ID, the launcher atomically installs immutable `psij_job.json`.
The receipt records its exact schema, run ID, submit token, executor, native ID, creation time, and executor work directory.
Readers require exact intent/receipt correlation and reject unknown fields, unsafe paths, invalid native IDs, mismatched commands, and mismatched launch configuration.
Neither artifact contains credentials, environment dumps, native scripts, or literal secret values.

Intent installation is the idempotency boundary.
If intent exists without a receipt, no recovery path may call PSI/J `submit()` again.
A failure after the external submit action begins but before receipt installation leaves the run `prepared`, appends stable `psij_submission_uncertain` backend metadata, and raises `PSIJSubmissionUncertainError`.
It does not invent a native ID, terminal outcome, or second scheduler job.
Explicit cancellation may terminalize that prepared run; any late orchestrator job then fails to acquire the already-terminal execution claim and cannot execute workflow code.

Receipt-backed monitoring reconstructs the configured executor, calls `attach(Job(), native_id)`, verifies native-ID correlation, and waits for a non-`NEW` observation for only a bounded interval.
Queued and active scheduler observations are secondary backend progress metadata.
A queued or active scheduler job remains launcher `prepared` until the orchestrator commits its normal startup claim.
Scheduler rejection, failure, cancellation, or completion before an orchestrator claim transitions the run to structured `failed`.
After the orchestrator claim, scheduler observations remain secondary; a terminal scheduler observation confirms backend absence only after the execution claim expires, then invokes the existing recovery rules, and workflow code is never rerun.
An unavailable or purged scheduler observation is recorded as unknown and does not imply launcher success, failure, cancellation, or loss.

Cancelling a receipt-backed `prepared` run first commits launcher `cancelled` and then best-effort cancels the exact PSI/J native job.
Cancelling `starting` or `running` preserves the existing graceful `cancel_requested` protocol and does not immediately cancel the scheduler job.
Only after `hard_cancel_after` may a controller attach and cancel an unresponsive PSI/J job.
Confirmed scheduler termination after that hard-cancel request follows the existing `lost` semantics because normal Parsl draining and cleanup are no longer proven.

The PSI/J launcher starts only the orchestrator job.
Parsl providers still allocate worker blocks.

### 17.11 Laptop-to-cluster submission transport

Remote submission from a laptop uses the system OpenSSH `ssh` and `sftp` executables to reach an installed `bioimageflow-cluster-agent` command on the cluster login node.
The command is one-shot: it reads exactly one bounded JSON request from standard input, writes exactly one protocol JSON response to standard output, writes diagnostics only to standard error, and runs no service.
The protocol schema is `bioimageflow.cluster.command.v1` and supports exactly `allocate-upload`, `commit-upload`, `submit`, `inspect`, `refresh`, `read-progress`, `read-logs`, `cancel`, and `prepare-result`.
Every mutable request carries a canonical UUID4 request ID, has an exact argument schema and canonical request digest, and stores an operation-scoped durable receipt.
Reusing a request ID with different arguments fails, while retrying the same request converges on the persisted result.
Unknown fields, versions, operations, duplicate JSON keys, invalid UTF-8, non-finite JSON, excessive size, excessive nesting, and excessive value count fail closed.
Responses use `bioimageflow.cluster.response.v1`, echo the request ID, and contain either an exact result or a stable structured error without tracebacks, credentials, environment dumps, or signed material.

`SSHSubmissionTransport` contains only an OpenSSH host alias or `user@host`, a normalized absolute POSIX staging root, one safe absolute remote executable token, and a positive finite connection timeout.
OpenSSH resolves user configuration, keys, agent, host-key policy, ports, and `ProxyJump`.
BioImageFlow invokes `ssh` and `sftp` with `shell=False`, explicit argument vectors, `BatchMode=yes`, normal host-key verification, bounded timeouts, and a small environment allowlist.
It provides no password handling, interactive authentication, private-key field, arbitrary SSH option list, host-key bypass, shell prelude, module command, environment escape hatch, or interpolation of request values into remote shell syntax.
SFTP uses one audited quoting function and writes only beneath a server-issued `.partial/<upload-id>/` path.

`LocalUpload(Path(...))` is the only marker that reads laptop filesystem content.
It is accepted only for a root workflow field port whose declared annotation is path-like.
Ordinary `Path` values are already cluster paths and retain their explicit normalized absolute POSIX spelling without laptop `expanduser()`, `resolve()`, or existence checks.
Ordinary strings remain byte-for-value strings even if they resemble paths.
Root DataFrames retain the existing logical digest and Parquet transport.
Each typed `Path` cell must already be a normalized absolute cluster path; relative typed paths and `LocalUpload` cells are rejected, while string cells remain strings.

One ready-bundle manifest covers the strict workflow graph or archive, invocation/configuration data, root DataFrame transports, and every explicit upload.
The workflow's absolute `storage_path` travels beside the workflow payload and remains the sole runtime storage path supplied to `Workflow.from_dict`; it never enters the graph/archive and is not an override.
Local upload roots preserve an NFC basename and contain only regular files and canonical directories with normalized POSIX relative names.
Symlinks, special files, traversal, excessive depth/count/bytes, duplicate names, Unicode/case collisions, and mutation between scan, read, and final stat are rejected.
The manifest records every file's size and SHA-256 digest and has its own canonical digest.

`allocate-upload` creates a distinct immutable upload ID and secure `.partial/<upload-id>/` directory.
`commit-upload` rejects missing, extra, symlinked, special, or tampered entries, atomically installs `ready/<upload-id>/`, and installs a revalidated read-only content-addressed object beneath `objects/sha256/<manifest-digest>/submission`.
No launcher run ID enters upload object identity.
The transport staging root is disjoint from `Workflow.storage_path`, including its launcher, cache, run-view, and output-view trees.
Installed objects must remain visible to the orchestrator and selected Parsl workers for every referencing run.
Operators may remove abandoned partial uploads and unreferenced objects only after their site's retention policy proves no live or retained run references them; this phase adds no general garbage-collection service or remote run store.

`submit` validates the committed bundle and cluster paths, materializes the workflow with the exact captured storage path, decodes only declared input types, validates Parsl and PSI/J configuration in the cluster environment, and then dispatches through the existing Phase 1b allocation and PSI/J launcher path.
Before launcher allocation it durably binds the submit request ID and digest to one preallocated UUID4 launcher run ID.
A retry opens that run when it already exists or resumes allocation with the same ID; concurrent equal requests cannot launch twice and a conflicting digest fails.
The existing PSI/J intent remains the external scheduler idempotency boundary, so response loss never authorizes a second orchestrator job.
`submit_workflow(..., transport=SSHSubmissionTransport(...))` requires an explicit `PSIJLaunchConfig`, uses the transport submission seam, and returns `RemoteWorkflowRun`.
The absence of transport preserves the exact cluster-local behavior and `WorkflowRun` return type.
`RemoteWorkflowRun.open()` accepts only the transport, normalized absolute cluster storage path, and run ID, validates the immutable submission and current status schemas, and claims no laptop-local `control_dir` or `view_dir`.
Every response repeats and validates the exact storage/run binding.

`inspect` is a read-only authoritative observation.
`refresh` delegates to the cluster-local `WorkflowRun.refresh()` and therefore performs only its existing launcher recovery and PSI/J reconciliation.
`read-progress` returns bounded pages after a global Phase 1b sequence and never creates client-side sequences.
`read-logs` returns bounded base64 raw stdout or stderr bytes by byte offset plus a stable file identity and first-page snapshot size, and explicitly reports replacement or truncation so the client restarts assembly before decoding with replacement.
Each call reads only that finite snapshot, so a concurrently growing log cannot make one client observation chase new bytes indefinitely.
`wait()` runs as interruptible laptop-side polling with a monotonic deadline; no cluster agent survives a disconnected request.
SSH or protocol loss is a transport error and does not change or guess launcher state.

`cancel` is a receipt-backed idempotent mutation that invokes only cluster-local `WorkflowRun.cancel()`.
Retries after response loss return the same durable mutation result, while later explicit observations remain authoritative across queued/start/finalizing/terminal races.

`prepare-result` requires launcher `succeeded`, validates the installed Phase 1b return and every exact immutable record locator without consulting `current.json`, and atomically creates or reuses an immutable content-addressed download object beneath transport staging.
Its final manifest binds schema, run ID, cluster storage path, public return shape, ordered mapping keys, root outputs, typed locators, record-asset mappings, every relative path, file kind, size, and SHA-256 digest.
The object copies exact Parquet frames, self-contained return assets, and only immutable record assets explicitly named by the return locators.
It copies no launcher control state, canonical cache record, current pointer, run view, or output view.

The laptop downloads the manifest and its exact files through SFTP into a unique sibling candidate.
It rejects absolute or traversing paths, symlinks, special files, missing or extra entries, digest mismatch, mutation, unsafe destination parents, and a pre-existing destination that is not the exact validated bundle for the same run.
Only after complete validation does it atomically install the destination.
The factored return loader resolves record-owned and return-owned cells beneath the verified download root, preserves declared external cluster Paths exactly, and reconstructs SharedArray values with fresh laptop-local owned backing.
Zero-output, failed, cancelled, lost, not-ready, corrupt, and pruned-result semantics remain those of the cluster-local `WorkflowRun`.

---

## 18. No-Shared-Filesystem Execution

The laptop submission transport in Section 17.11 does not require the laptop to share the cluster filesystem.
No-shared-filesystem execution remains a later portability milestone specifically for sites where the orchestrator and Parsl workers do not share paths.

It requires a schema-aware `ArtifactStager` with these logical operations:

- map canonical path inputs to worker-readable paths or URIs,
- create worker-local `ExecutionContext` directories,
- transfer verified tool-source bundles when required,
- collect declared file and canonical directory-tree assets,
- map returned worker paths to record-relative owned assets or, after the storage contract defines URI identity, durable external URIs,
- preserve scalar values exactly,
- clean worker-local scratch independently from canonical cache attempts.

Only fields declared path-like by tool schemas are rewritten.
Arbitrary strings are never treated as paths heuristically.

Worker-local values MUST NOT enter:

- result-key material,
- persisted DataFrames,
- `manifest.json`,
- run return snapshots,
- downstream canonical arguments.

No-shared-filesystem acceptance requires archive custom-source transfer or exact installed-module resolution without an orchestrator path.
Before this milestone, the normative storage contract MUST define any durable-URI external-reference kind, validation, and cache identity.
URI values MUST NOT reuse normalized-absolute-path identity implicitly.

---

## 19. Optional Future Platform Changes

### 19.1 Remote DataFrameTool

Remote `DataFrameTool` execution requires a separate specification and platform update.

That specification must define:

- opt-in metadata and its wire serialization,
- whether `merge_dataframes` and `transform` move together,
- whole-frame versus partition-safe execution,
- dynamic output schemas,
- DataFrame and index serialization,
- a tool-facing context if remote DataFrameTools may create assets,
- the full `bioimageflow` worker environment and Python `>=3.10` baseline,
- cache and artifact publication.

Until then every DataFrameTool is a local barrier.

### 19.2 Streaming

Streaming means downstream computation begins before an upstream node is finalized.

It is distinct from live progress, logs, or status polling.

A future streaming specification must define partial accumulators, row readiness, lineage matching, barrier nodes, failure rollback, and cache finalization.

`compute_steps()` remains node-level and is not a streaming API.

---

## 20. Acceptance Tests

### 20.1 API, lifecycle, and planning

- Parsl is not imported by ordinary package import, validation, serialization, or `plan()`.
- Missing Parsl produces a clear optional-extra error.
- `Workflow.create_engine()` supports Parsl and rejects backend-inapplicable arguments.
- Direct-engine creation accepts only its documented no-resource lifetime policy.
- Explicit engine injection overrides the stored workflow preference and applies the documented `workflow`/fixed execution-policy precedence.
- Run metadata records the effective Parsl engine after explicit injection.
- Constructor side effects are absent.
- `execution`, `engine`, and `external` lifecycle policies match their ownership contracts.
- `close()` and context-manager cleanup are idempotent.
- Executing a closed engine fails.
- Closing a steps generator applies cleanup.
- External DFK cleanup never affects caller or unrelated tasks.
- One engine rejects overlapping executions while still overlapping independent branches within one execution.
- Failure, cancellation, and `close()` drain every submitted future before engine reuse or owned-resource cleanup.
- All five `NodePlanStatus` values remain available without Parsl startup.

### 20.2 Recursive workflows and scheduling

- Nested `WorkflowNode` internals dispatch with stable scoped names.
- A WorkflowNode boundary is never submitted remotely and owns no cache record.
- A downstream result key crossing a workflow boundary contains flattened selected-provider record references and selectors, never a boundary diagnostic signature.
- Compilation preserves provider/selector recipes and runtime resolution makes a downstream path non-reusable while any consumed published provider lacks a selected immutable record.
- A non-reusable real-tool path executes with `result_key=None`, creates no record/current/run-record pointer, and confines owned outputs to the specified transient tree.
- Stable workflow input/output IDs survive archive round-trip and execution.
- Incompatible published indexes fail boundary assembly, while output renaming preserves stable-ID connectivity and changes whole-DataFrame identity.
- Detached enabled terminals run before boundary success.
- Detached terminal failure and cancellation fail or cancel the boundary.
- Detached completion-only record changes do not invalidate consumers of unchanged published outputs.
- Zero-output workflows run terminals and return an empty DataFrame.
- Aggregate plan entries follow the exact `SKIPPED`/`CACHED`/`PENDING_UPSTREAM`/`UNEXECUTED` recursive contract.
- `compute_steps()` includes disabled ordinary tool steps as skipped but does not expand a disabled workflow subtree.
- Parallel policy overlaps independent ProcessingTool nodes.
- Sequential policy permits one node and one in-flight row task at a time.
- DataFrameTool always runs in the orchestrator.

### 20.3 Dispatch semantics

- Source ProcessingTool uses row index `"0"`.
- Out-of-order futures produce input-ordered DataFrames.
- Single and multiple row outputs preserve order and index explosion.
- Empty row outputs produce no sentinel rows.
- Reserved `::` source indices are rejected.
- Coarse/fine lineage expands correctly.
- Unrelated roots and divergent sibling explosions raise `IndexAlignmentError`.
- Flat `list[Outputs]` and nested batch returns normalize consistently across all engines.
- Batch cardinality errors are consistent across all engines.
- Empty aligned batches skip by default.
- `run_empty_batch`, anchors, zero-row templated assets, and scalar outputs match direct execution.
- Bounded submission never exceeds the effective in-flight limit.
- Explicit chunking preserves positions and deterministic output.
- Worker-return dictionaries are reconstructed and validated into the same canonical output shape across all engines.
- Reusable and transient tasks echo exact task, invocation, and optional cache-attempt correlation.
- Malformed, missing, extra, duplicated, wrongly typed, or future-version task and result fields fail closed.

### 20.4 Executor and tool origins

- Missing, ambiguous, and unattested routes fail before processing tasks.
- Every ResourceSpec fits the attested homogeneous worker slot or is rejected.
- Shared-filesystem probe detects unreadable, unwritable, or differently mounted roots.
- Core version/API mismatch fails preflight.
- Installed-module origin requires exact distribution metadata, version, module, and class.
- Versioned-module origin distinguishes two versions in one workflow.
- Shared project modules resolve relative imports and helpers.
- Raw source-file fallback works only on a verified shared path.
- Archive source bundles preserve source ID/hash and helper files.
- Archive source materialization requires and verifies the configured absolute shared runtime root.
- Tampered archive bundles fail preflight.
- Equal class names from different origins retain separate worker instances.
- No task installs a package or starts a Wetlands worker.

### 20.5 Cache, paths, and views

- Direct, Wetlands, and Parsl produce the same result keys for equivalent selected inputs.
- Attached and submitted root DataFrame inputs produce the same result key from one canonical logical digest regardless of transport path or Parquet bytes.
- Canonical DataFrame digests are independent of incidental Parquet bytes.
- Record manifests validate logical DataFrame identity and Parquet transport integrity separately.
- Successful execution publishes one valid immutable record and guarded current selection.
- Failed and cancelled nodes select no partial record.
- Concurrent first-valid publication selects one record safely and reports conflicts.
- A competing selected record is loaded and used downstream.
- Corrupt current pointers are not repaired silently.
- Persisted owned path cells are record-relative.
- Runtime cache-hit paths are absolute beneath the selected record.
- Cache-hit path rehydration rejects absolute, traversal, and symlink escapes.
- Attempt, work, worker-local, and output-view paths never enter a record.
- File and directory assets use explicit asset types and canonical tree validation in `manifest.outputs`; zero-row assets and scalar metadata follow the same record contract.
- `views/runs`, `views/latest`, latest-success, and optional output projections match the canonical storage contract.
- Scoped recursive node paths are safe in run and output views.
- Active workflow run IDs appear in publication provenance.
- Run IDs and launcher paths appear in no result-key or record-ID material.
- Attempts are not removed while a late task can write.

### 20.6 Progress, errors, and cancellation

- Only public BioImageFlow statuses appear in `ProgressEvent.status`.
- Scoped names and result/record identities are correct.
- Callback invocations are serialized while independent nodes interleave.
- Row-complete events are zero-based and aligned-order for row/chunk dispatch; whole-node batch emits none.
- Backend task metadata is persisted separately.
- Remote exceptions preserve traceback and full task identity.
- Deterministic primary failure selection uses the workflow-wide compiled-node/position/task ordering and is independent of completion race.
- Opaque Parsl retries are rejected in Phase 1.
- Cancellation stops submission, requests future cancellation, ignores late results, and preserves earlier records.
- A non-cancellable writer leaves an unselected attempt, is drained before reuse, and becomes cleanup-eligible only after termination.
- Idle cancellation does not cancel the next compute.
- Root interface and stepped execution share the active cancellation context and do not inherit a stale token.

### 20.7 Submitted runs

- Local submission allocates one run ID used by launcher state and library views.
- Launcher control artifacts remain under `launcher/v1/runs/<run-id>` and canonical portable views remain under `views/runs/<run-id>`.
- The normative storage contract reserves the launcher namespace and defines its confinement and retention before Phase 1b ships.
- Manual submission writes a reproducible command and remains reconnectable.
- Root constants use canonical serialization.
- Root DataFrame inputs round-trip through canonical-digest-verified Parquet references.
- Archive custom sources are included once and materialized on shared storage.
- Partial workflows and unresolved tools fail before launch.
- Run allocation and execution claiming reject collisions and concurrent orchestrators.
- Expired pre-start claims can be recovered without execution, while a post-start takeover never reruns workflow code.
- Versioned status transitions use guarded revisions and progress sequences are monotonic.
- Client restart can reconnect from only the run ID and storage root.
- Cancellation marker reaches `Workflow.cancel()`.
- Prepared cancellation terminates directly, status remains the cancellation source of truth, and the marker is only a wake-up hint.
- Cancel-versus-success races are closed by the non-cancellable `finalizing` claim; optional hard termination after the graceful interval produces `lost`.
- Single-target, multi-target, root boundary, renamed public output, and zero-output return shapes reload correctly.
- Workflow-boundary results load without an aggregate cache record.
- Historical result loading does not depend on the record remaining current.
- Return installation is atomic before either success status, every record-backed path cell has an exact immutable-record locator, and missing pruned records raise the stable result-unavailable error.
- Run-transient owned return assets are copied into self-contained return assets and retain no transient paths.
- Recovery converges correctly after a crash at every return/canonical-status/launcher-status boundary.
- PSI/J submission constructs one exact scheduler job, persists intent before external submit and a correlated native receipt after success, and remains duplicate-safe across every crash boundary.
- PSI/J reattachment observes and cancels by exact native ID after process restart; queued jobs remain `prepared`, early terminal jobs fail, and missing runtime or executor descriptors raise the stable backend-not-supported error.
- Active PSI/J cancellation remains graceful until the hard-cancel boundary, after which confirmed termination records `lost`.
- Direct Slurm, PBS, LSF, and OAR launcher backend aliases are rejected; PSI/J supports only its declared Slurm, PBS, and LSF executors.

---

## 21. Resolved Design Decisions

1. BioImageFlow has one distributed task engine: `ParslEngine`.
2. Parsl providers allocate workers; launcher adapters start only orchestrators.
3. The strict workflow graph is not extended with live execution configuration.
4. Explicit engine injection is the canonical attached cluster API.
5. Phase 1 executors are preinstalled, explicitly attested, and routed as homogeneous worker slots.
6. Wetlands task dispatch is never nested inside Parsl.
7. Worker tool identity is origin-aware across installed, versioned, shared, source-file, and archive modules.
8. DataFrameTool remains local until a separate platform contract changes it.
9. Row task size defaults to one; chunking is explicit.
10. `process_batch` remains whole-node unless a future tool contract declares partition safety.
11. Parsl app caching is disabled; BioImageFlow v1 is the result cache.
12. Opaque Parsl retries are disabled in Phase 1.
13. Public progress statuses remain backend-neutral.
14. `ImageShared` is rejected only when a value would cross a remote task boundary.
15. `manifest.outputs` is the artifact authority; no second artifact sidecar is introduced.
16. Persisted record-owned paths are relative and runtime paths are rehydrated from immutable records.
17. Submitted runs share the canonical BioImageFlow run ID while launcher control/binary artifacts remain outside portable run views under a namespace that must first enter the storage contract.
18. Submitted results persist the public return shape because workflow boundaries own no cache record.
19. Streaming is not part of step execution or Phase 1.
20. Workflow-boundary diagnostic signatures never substitute for selected-provider record references in downstream cache identity.
21. One Parsl engine has one active execution and drains every submitted future before reuse or cleanup.
22. Phase 1 exposes neither task retries nor task timeouts; those require separately specified visible semantics.
23. A missing reusable cache identity executes through an explicitly non-canonical transient path; diagnostic hashes never create reusable records.
24. Submitted success claims the non-cancellable `finalizing` state before canonical success and launcher success are committed.
25. `ResourceLifetime` and `resource_lifetime` are the only public resource-ownership names.
26. Every processing task carries a required invocation ID, an optional reusable-only cache attempt ID, and a task ID.
27. Logical DataFrame identity is independent of Parquet transport bytes.

---

## 22. External Parsl Assumptions

Phase 1a supports `parsl>=2026.5.25,<2026.6`.
The development lock selects a release in that range and the complete Parsl matrix runs against it.
Parsl requires Python `>=3.10`, matching the orchestrator; `bioimageflow-core` remains Python 3.9-compatible and independent of Parsl.

This design relies only on documented Parsl capabilities:

- explicit `Config` objects and multiple labeled executors,
- Python apps bound to an explicit DataFlowKernel and executor labels,
- serializable app inputs and outputs,
- AppFuture result/exception/cancel interfaces,
- DFK cleanup for engine-owned resources,
- providers for worker-block allocation,
- executor-specific resource specifications where documented.

Parsl cancellation is best-effort; a running future may not cancel.
Python-app stdout/stderr capture is not assumed to be universal.
Per-task resource dictionaries are treated as executor-specific.

Relevant upstream documentation:

- [Parsl configuration and multiple executors](https://parsl.readthedocs.io/en/stable/userguide/configuration/config.html)
- [Parsl Python apps](https://parsl.readthedocs.io/en/stable/userguide/apps/python.html)
- [Parsl DataFlowKernel API](https://parsl.readthedocs.io/en/stable/stubs/parsl.dataflow.dflow.DataFlowKernel.html)
- [Parsl AppFuture API](https://parsl.readthedocs.io/en/stable/stubs/parsl.dataflow.futures.AppFuture.html)
- [Parsl executor overview](https://parsl.readthedocs.io/en/stable/userguide/configuration/executors/)
