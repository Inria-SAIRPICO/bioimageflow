# Parsl Phase 1a Implementation Guide

Status: implemented attached Parsl engine plan pending final validation and the final GUI-update handoff.

## 1. Authority and Reading Order

This document records the dependency order, ownership, contract gates, and acceptance gates used to implement Phase 1a.
It does not replace the normative behavior in:

- `docs/source/specs.md`,
- `docs/source/reference/unified_workflow_contract.md`,
- `docs/source/reference/output_cache_storage.md`,
- Sections 2.1, 3 through 16, 20.1, 20.2, and 21.1 through 21.6 of `docs/parsl_distributed_engine_specs.md`.

The platform contracts win if this guide conflicts with them.
The distributed-engine specification wins for Parsl-specific behavior that the platform contracts have already been extended to permit.
Sections 17 through 19 and acceptance Section 21.7 of the distributed-engine specification are not part of Phase 1a.

Section 5 records the final-design decisions applied by the implementation.
The normative platform contracts contain the resulting public and cross-engine behavior.

## 2. Outcome and Definition of Done

Phase 1a delivers an attached, shared-filesystem execution backend.
A caller constructs a runtime-only `ParslEngine`, attaches it to the current Python process, and passes it explicitly to `Workflow.compute()` or `Workflow.compute_steps()`.
The orchestrator compiles the workflow, resolves cache identity and arguments, executes every `DataFrameTool` and `WorkflowNode` boundary locally, dispatches reachable `ProcessingTool` calls through Parsl, and publishes through the final cache and view contracts.

The feature is done only when all of the following are true:

- The contract gates and shared direct/Wetlands prerequisites are complete.
- The public attached API, optional dependency behavior, lifecycle policies, and engine factory match the distributed-engine specification.
- Recursive workflows, disabled nodes, planning, index lineage, empty and zero-row behavior, output validation, progress, cancellation, cache records, and views have cross-engine parity.
- Every supported tool origin and executor route is validated before processing submission.
- Submission is bounded, result and progress order are deterministic, failure selection is deterministic, and every submitted future is drained before engine reuse or owned-resource cleanup.
- A real local Parsl executor test covers the control plane, and a process-isolated local Parsl executor test covers serialization, imports, worker state, and shared paths.
- Every applicable item in distributed-engine acceptance Sections 21.1 through 21.6 has a named automated test or an explicitly documented environment-dependent test.
- The documented repository validation commands pass.
- The final source tree, tests, and normative documentation describe only the chosen architecture and contain no superseded implementation path, compatibility shim, deprecated alias, fallback reader, dual writer, or historical design commentary.
- A concise GUI-update guide is written only after the final library API, events, storage, run views, and output views are stable.

A single successful `process_row` example is an integration milestone, not the Phase 1a completion criterion.

## 3. Phase Boundary

### 3.1 Included

Phase 1a includes:

- explicit `ParslEngine` injection into `compute()` and `compute_steps()`,
- `"parsl"` as a serializable workflow engine preference,
- `Workflow.create_engine()` support for runtime-only Parsl arguments,
- lazy loading through the `bioimageflow[parsl]` optional extra,
- attached execution with either an engine-owned `parsl.Config` or a caller-owned DataFlowKernel,
- remote `ProcessingTool.process_row()` calls, explicit row chunks, and one whole-node `process_batch()` call,
- local `DataFrameTool` execution and local recursive boundary assembly,
- shared absolute storage, attempt, transient, source, and archive-materialization paths,
- explicit executor bindings, environment attestations, node/environment routing, resource-capacity checks, and per-executor preflight,
- installed-module, versioned-module, shared-module, source-file, and materialized-archive-module worker origins,
- the final `"execution"`, `"engine"`, and `"external"` resource-lifetime vocabulary,
- context-scoped cancellation, future cancellation requests, complete future draining, and safe attempt retention,
- direct/Wetlands/Parsl parity for shared cache identity, publication, views, output validation, failure selection, and progress.

### 3.2 Excluded

Phase 1a does not include:

- a submitted orchestrator process, `WorkflowRun`, or reconnectable result protocol,
- local, manual, or scheduler-specific launcher backends,
- direct BioImageFlow submission of rows or nodes to a site scheduler,
- staged or no-shared-filesystem transfer, mount translation, or durable-URI staging,
- remote `DataFrameTool`, dataframe partitioning, or partial-result streaming,
- package installation or environment creation inside Parsl tasks,
- nested Wetlands dispatch from Parsl workers,
- worker-side cooperative cancellation,
- Parsl-managed retries, a retry-count API, or a Parsl task-timeout API,
- automatic chunk growth, chunk-to-row fallback, or split `process_batch()`,
- a generic `parsl_resource_specification` adapter,
- Parsl app caching, a second result cache, or global Parsl loader/cleanup ownership,
- universal worker stdout/stderr capture or worker-originated live progress,
- GUI implementation work or preservation of GUI assumptions while the library is being designed and implemented.

Parsl providers may allocate worker blocks for an attached engine.
That does not turn provider configuration into a BioImageFlow launcher API.

## 4. Implemented Repository Structure

| Area | Final owner | Implemented contract |
|---|---|---|
| Processing backend seam | `bioimageflow/backends.py` and `bioimageflow/parsl/backend.py` | Immutable dispatch requests carry run, cache or transient, aligned-index, resource, and cancellation identity without moving scheduling into a backend. |
| Scheduling and graph execution | `bioimageflow/engine/` | Deterministic compiled order, active-execution enforcement, bounded dispatch, shared output validation, optional cache identity, progress, and failure selection. |
| Workflow runtime | `bioimageflow/workflow/runtime.py` and `bioimageflow/workflow/execution_context.py` | Explicit engine injection, one active execution, cancellation context propagation, root-wrapper propagation, and effective-engine run metadata. |
| Cache and storage | `bioimageflow/cache/` and `bioimageflow/storage/` | Canonical logical dataframe identity, selected-provider recipes, non-reusable transients, directory assets, atomic immutable-record installation, first-valid selection, run views, output views, and separate diagnostics. |
| Custom sources and loading | `bioimageflow/workflow/custom_sources.py`, `bioimageflow/tool_loader.py`, `bioimageflow/env_manager.py`, and `bioimageflow-core` | Strict worker origins, verified archive materialization, executor preflight, and one processing task/result protocol shared by remote backends. |
| Focused tests | `tests/unit/parsl/`, `tests/integration/parsl/`, worker-protocol tests, storage tests, and cross-engine suites | Pure, controlled-future, real ThreadPoolExecutor, and process-isolated HighThroughputExecutor tiers with permanent acceptance traceability. |
| Development guardrails | File-size, import-boundary, affected-test, and split-CI tooling | Every new production path has focused ownership and remains within the repository guardrails. |

Scheduler, cache, publication, progress, and graph logic remain in their focused platform owners.

## 5. Final-Design Policy and Contract Gates

These gates precede implementation beyond independent test scaffolding.
WP0 updates every normative contract to the final chosen model before downstream code is merged.

### 5.1 Clean final architecture

Implementation targets only the final normative design.

- Preserve an API or behavior only when it is deliberately selected as part of the final library contract, never merely because an earlier implementation exposed it.
- Do not add compatibility shims, deprecated aliases, fallback readers, dual writers, schema translation, or migration code.
- When a shared path is replaced, migrate direct, Wetlands, and Parsl to the final path and delete the superseded implementation, tests, comments, and documentation in the same work package.
- Existing cache/storage contents are disposable development data and receive no read or migration support.
- Tests create fresh storage under the final contract and do not carry fixtures for superseded cache or worker formats.
- Final normative documentation describes only the resulting system and contains no implementation history or superseded architecture.
- The library implementation does not preserve GUI assumptions or coordinate changes with GUI code.
- After the library and all acceptance gates are final, WP10 writes a short GUI-update guide describing the final integration surfaces and the high-level changes a GUI agent must apply.

The final guide is a handoff document, not a compatibility layer or a reason to constrain the library design.

### 5.2 Public platform contract

Update `docs/source/specs.md` to describe:

- the `"parsl"` engine preference,
- explicit attached engine construction and factory arguments,
- backend-specific argument rejection and engine-injection precedence,
- public Parsl configuration types and `ParslTaskError`,
- `WorkflowExecutionContext`, `run_context=`, one-active-workflow execution, and context-scoped cancellation,
- optional reusable cache identity and non-reusable downstream execution,
- canonical root-dataframe identity,
- shared output/cardinality validation,
- deterministic workflow-wide failure selection,
- effective-engine and active-run publication provenance.

WP0 also applies the final backend-neutral names `ResourceLifetime` and `resource_lifetime` to direct, Wetlands, and Parsl resource ownership.
No alternate lifetime type, parameter alias, or backend-specific ownership name remains.

Do not change the strict recursive graph or archive schema except for accepting `"parsl"` in the existing engine preference field.

### 5.3 Non-reusable `ProcessingTool` storage

Extend `docs/source/reference/output_cache_storage.md` with the final tree:

```text
cache/v1/transient/runs/<run-id>/nodes/<node-key>/<invocation-id>/
  assets/
  work/
```

Before implementing that tree, the storage contract must define:

- safe run, node, and invocation identifiers,
- path confinement and symlink/traversal rejection,
- ownership of assets and work paths,
- how long attached results remain usable,
- cleanup eligibility and active-writer protection,
- whether and where non-record run diagnostics are written,
- the prohibition on records, `current.json`, latest pointers, run-node record pointers, and output projections for this path.

The implementation does not fabricate a result key or use a workflow diagnostic signature to obtain a normal cache attempt.

### 5.4 Directory-asset contract

Define and implement one final reusable-record directory-asset contract:

- manifest entries use `kind="owned_asset"` and an explicit `asset_type` of `"file"` or `"directory"`,
- a directory digest is computed from canonical sorted relative entries containing entry type, relative path, file size, and file digest,
- symlinks, special files, duplicate normalized paths, traversal, and case-normalization collisions are rejected,
- record validation and cache-hit rehydration,
- run/output-view materialization behavior.

The final implementation contains one directory-asset path, not separate local and distributed variants.

### 5.5 Processing task correlation

WP0 revises the worker protocol before implementation:

- `invocation_id` is required for every processing-node execution and identifies its reusable or transient workspace,
- `cache_attempt_id` is optional and is present only when a reusable result key created a cache attempt,
- `task_id` identifies one submitted row chunk or whole-node batch task,
- task results echo and validate all three fields exactly.

No field is overloaded with two meanings.

### 5.6 Canonical dataframe artifact contract

Publication uses the canonical logical dataframe digest for record identity and a separately named Parquet transport digest for file-integrity validation.
The manifest schema contains both meanings explicitly.

Choose, document, and test the writer settings used for the supported pandas/PyArrow range.
Root dataframe identity uses the same canonical logical digest.
No reader, writer, fixture, or documentation for the superseded manifest meaning is retained.

## 6. Package and Responsibility Boundaries

The orchestrator remains Python `>=3.10` and owns graph compilation, pandas objects, validation, caching, publication, routing, task submission, progress, and cancellation.

`bioimageflow-core` remains Python `>=3.9`, keeps NumPy as its only dependency, and owns worker-safe envelopes, origin decoding/loading, output-to-plain-value conversion, and top-level worker entry points.

Core worker code must not import pandas, Pydantic, Parsl, or orchestrator graph modules.
Use `Optional[...]` and other Python 3.9-compatible annotations in core protocol types.

Apply the repository's 800-line production-module ceiling to both orchestrator and core Python modules.
Extend the import-boundary guard so storage, cache, and shared engine modules cannot import `bioimageflow.parsl` or the external `parsl` package, core cannot import either package, and no production module performs a top-level external `parsl` import.
Only functions inside the focused `bioimageflow.parsl` implementation may load the optional external package, after a caller selects Parsl behavior.

The orchestrator resolves every constant, default, dataframe binding, absolute runtime path, output template, and `ExecutionContext` before dispatch.
The worker receives plain resolved values and a worker origin; it never receives the graph-construction tool instance or workflow graph.

`DataFrameTool.merge_dataframes()` and `transform()` always run in the orchestrator.
`WorkflowNode` expansion, completion gates, and boundary dataframe assembly always remain in the orchestrator.

Parsl owns executor/provider behavior and transport.
BioImageFlow owns only the futures submitted by the current `ParslEngine` execution.

## 7. Implementation Ownership and Worktrees

### 7.1 File ownership

The final implementation follows the focused package structure and repository size limits.

| File or area | Expected responsibility |
|---|---|
| `docs/source/specs.md` and the normative reference documents | Final public, execution, recursive, cache, storage, and worker contracts from Section 5 of this guide. |
| `packages/bioimageflow/pyproject.toml` and `uv.lock` | `bioimageflow[parsl]`, selected supported Parsl range, and locked development/test dependency. |
| Root `pyproject.toml` | `parsl` pytest marker, development dependency selection, and test-tier configuration. |
| `scripts/check_file_sizes.py` and `scripts/check_import_boundaries.py` | Core production-module size coverage and the optional-Parsl dependency boundaries from Section 6. |
| `bioimageflow/parsl/` | Focused modules for public value types, engine lifecycle, backend dispatch, routing, startup/preflight, archive materialization, and errors; no module may exceed the orchestrator size ceiling. |
| `bioimageflow/backends.py` | Small backend-neutral protocol plus direct and Wetlands adapters; `ProcessingDispatch` remains scheduler-owned resolved input. |
| `bioimageflow/workflow/execution_context.py` | Public `WorkflowExecutionContext`, cancellation, active binding, and terminal finalization state. |
| `bioimageflow/engine/` | Compiled scheduling, deterministic failures, shared output validation, optional cache identity, argument resolution, dataframe construction, and backend orchestration. |
| `bioimageflow/workflow/runtime.py` | Engine preference/factory, active-context binding, root-wrapper propagation, effective engine metadata, and public compute signatures. |
| `bioimageflow/cache/identity.py`, `cache/processing.py`, `cache/dataframe.py`, and `cache/metadata.py` | Provider recipes, logical identity, reusable publication, active-run provenance, and shared cache metadata. |
| `bioimageflow/storage/identity.py`, `storage/manifests.py`, `storage/repository.py`, `storage/run_views.py`, and `storage/output_views.py` | Final logical/transport digests, transient paths, directory assets, record validation, and public views. |
| `bioimageflow/worker_origins.py` and `bioimageflow/env_manager.py` | One orchestrator-side origin model used by remote backends, without provisioning Parsl environments through Wetlands. |
| `bioimageflow-core/bioimageflow_core/worker_protocol.py` | Python 3.9-safe task/result/origin envelopes, strict decoding, canonical origin identity, and preflight request/result values. |
| `bioimageflow-core/bioimageflow_core/worker_origins.py` | Strict origin loading and the origin-keyed worker-instance cache. |
| `bioimageflow-core/bioimageflow_core/worker.py` | The top-level processing entry point used by Wetlands and Parsl. |
| Package `__init__.py` files | Final explicit public exports and lazy optional-dependency behavior, without deprecated aliases. |
| `tests/unit/parsl/` and `tests/integration/parsl/` | Focused Parsl value, lifecycle, routing, preflight, dispatch, semantics, and real-runtime tests. |
| Existing focused test packages | Cross-engine regression coverage selected through `tests/ownership.toml`. |
| `docs/parsl_phase1a_gui_update_guide.md` | Concise final GUI-agent handoff created only in WP10 after every library acceptance gate passes. |

Avoid concentrating the Parsl implementation in one module.
Any observable rule that also applies to direct or Wetlands belongs in a shared helper or engine path and needs cross-engine tests.

### 7.2 Integration worktree

All Phase 1a implementation is integrated in:

```text
.worktrees/parsl-phase1a
branch: feature/parsl-phase1a
base: 6a3f67b
```

The primary working tree remains on `main`.
The integration agent owns the feature branch, master plan, merge order, final public façades, `uv.lock`, `tests/ownership.toml`, and cross-package validation.

### 7.3 Parallel agent worktrees

Create a child worktree only for a concrete independent slice based on the latest integration commit.
Use short-lived branches and paths such as:

```text
.worktrees/parsl-core-protocol
.worktrees/parsl-public-types
.worktrees/parsl-storage
```

Rules:

- one writing agent owns one child worktree,
- no two agents edit the same ownership area concurrently,
- child agents commit only their scoped production and test changes,
- child agents report contract discoveries to the integration agent instead of changing the master plan independently,
- the integration agent reviews and merges one validated commit series at a time,
- conflict-heavy façades, dependency locks, test ownership, and final documentation remain integration-agent work,
- after merge, move the child worktree to trash, run `git worktree prune`, and delete the merged branch.

Use at most two parallel implementation agents plus the integration agent.
The remaining agent slot is reserved for read-only review, race analysis, or focused test audit.

Parallel work is appropriate after WP0 for:

- the core worker protocol and origin model,
- public Parsl value types plus optional-dependency tests,
- isolated testkit fixtures and acceptance-test scaffolding.

Shared scheduler, workflow context, cache identity, publication, cancellation, and future-drain changes are integrated sequentially unless their interfaces and file ownership have already been frozen by a completed gate.

### 7.4 Plan-update cadence

The integration agent updates the master plan:

- after every work-package exit gate,
- before creating child worktrees from a new integration baseline,
- immediately when a normative decision changes downstream interfaces or test ownership,
- after merging parallel branches and before starting their consumers.

Do not update the plan after every file or individual test.
Each checkpoint records completed commits, focused validation, remaining blockers, and any changed downstream dependencies.

## 8. Delivery Sequence

Use the following dependency order.
Later packages may be developed behind tests, but they must not be integrated as a shortcut around an earlier exit gate.

| Work package | Deliverable | Exit gate |
|---|---|---|
| WP0 | Final contract updates and regression baseline | Section 5 of this guide is fully normative and direct/Wetlands tests assert only the chosen final behavior. |
| WP1 | Worker-safe protocol and origin model | Core imports on Python 3.9 and strict round-trip/origin tests pass. |
| WP2 | Shared execution context and extended backend seam | Direct/Wetlands lifecycle, steps, cancellation, validation, and failure behavior pass through the final context and dispatch contracts. |
| WP3 | Shared cache identity and storage prerequisites | No boundary diagnostic key remains; canonical digests and reusable/non-reusable paths follow the storage contract. |
| WP4 | Public Parsl API and side-effect-free lifecycle | API, packaging, lazy import, constructor, factory, ownership, and close tests pass without processing submission. |
| WP5 | Compile, route, materialize, acquire, and preflight | Invalid runs fail before processing; a known fully cached run needs no DFK. |
| WP6 | One-row bounded dispatch and deterministic collection | Real local Parsl rows match direct output/cache identity and never exceed the window. |
| WP7 | Chunk, batch, empty, validation, and recursive parity | Shared semantics pass for direct, Wetlands, and Parsl. |
| WP8 | Progress, errors, cancellation, deterministic failure, drain, and cleanup | Failure/cancellation cannot publish partial state or leave an engine reusable while a writer is active. |
| WP9 | Acceptance and release hardening | Distributed-engine acceptance Sections 21.1 through 21.6 and repository validation pass. |
| WP10 | Final GUI-update guide | A concise handoff describes the final public API, events, storage/view surfaces, and high-level GUI work without constraining or changing the library. |

## 9. WP0: Contracts and Regression Baseline

Land the Section 5 contract updates from this guide first.
Update direct and Wetlands tests to assert the final shared behavior before adding Parsl dispatch.

The baseline must cover at least:

- recursive scoped names, stable workflow ports, published-index compatibility, detached terminal completion, zero-output workflows, and aggregate planning,
- disabled ordinary steps versus disabled workflow subtrees in `compute_steps()`,
- all five `NodePlanStatus` values without resource startup,
- direct/Wetlands lifecycle and environment-manager reuse,
- source, one-to-one, exploded, empty, batch, and zero-row processing outputs,
- cache hit, publication, guarded first-valid selection, selected competing record use, and corrupt-pointer failure,
- path canonicalization and rehydration for owned assets versus declared external paths,
- progress callback serialization and public status vocabulary,
- shared-memory cache behavior that remains valid for local execution.

Do not weaken the final platform behavior to make Parsl implementation easier.
When a direct or Wetlands path differs from the final normative contract, replace it and delete tests for the superseded behavior.

## 10. WP1: Worker Protocol and Tool Origins

### 10.1 Protocol types

Add the exact versioned worker-safe types from distributed-engine Sections 9.2, 9.3, and 12.1:

- `ProcessingTaskV1`,
- `RowInvocationV1`,
- `ProcessingTaskResultV1`,
- `RowResultV1`,
- `InstalledModuleOriginV1`,
- `VersionedModuleOriginV1`,
- `SharedModuleOriginV1`,
- `SourceFileOriginV1`,
- `ArchiveModuleOriginV1`,
- `WorkerToolOriginV1`.

Invocation decoders must reject an unknown schema, kind, mode, missing key, extra key, duplicate position, invalid scalar type, and malformed path or hash before tool code runs.
Result decoders apply the same strictness after worker execution and before any result is accepted or published.
Boolean values must not pass integer-only validation accidentally.
Task results must echo and exactly match task ID, scoped node, execution attempt/invocation correlation, retry number, mode, row positions, and row-index strings.

`task_retry` is fixed at zero in Phase 1a.
It is distinct from the node attempt or invocation identifier.

### 10.2 Origin construction and loading

Implement one strict worker-origin resolver and processing entry point for every remote backend.

Origin resolution rules are:

- `installed_module` verifies the exact installed distribution metadata, version, module, and class,
- `versioned_module` carries both distribution and explicit `import_package`, plus version, canonical/scoped module names, verified store root, and class,
- `shared_module` carries an importable module, verified shared import root, source hash, and class,
- `source_file` carries a verified absolute shared file, source hash, and class,
- `archive_module` carries source ID/hash, canonical/scoped module names, verified materialization root, and class.

A distribution name and an import-package name are different identities and must not be inferred from each other.
The origin builder obtains the true distribution from explicit installation metadata or verified distribution metadata.
If installed distribution metadata is unavailable, installed-module mode fails and the deployment must select another origin variant.

Every filesystem value is normalized, absolute, confined to its declared shared root, and verified before processing submission.
An orchestrator-only `sys.path` entry is not a worker deployment path without successful shared-path preflight.

Canonical worker instance identity is the SHA-256 digest of canonical JSON for the complete origin, including the class name.
Do not cache worker instances by only class name, module name, or source filename.
This worker deployment identity is separate from logical cache tool identity, which continues to use the platform's tool/version/source-hash contract and excludes runtime materialization and import roots.

The core entry point imports inside the worker, creates or reuses the origin-specific tool instance, invokes `process_row()` or `process_batch()`, and returns plain dictionaries.
It does not construct pandas objects or install packages.

### 10.3 Core exit tests

Unit tests must prove:

- the module parses and imports under the core Python 3.9 compatibility test,
- importing it does not import pandas, Pydantic, Parsl, or orchestrator modules,
- each envelope and origin has an exact successful round trip,
- malformed and future-version payloads fail closed,
- two versions, two archives, and equal class names from different origins use separate instances,
- installed distribution metadata mismatch fails,
- source hash mismatch and path escape fail,
- Wetlands and Parsl both use the final protocol, origin resolver, and worker-instance identity,
- no tuple-based entry point, informal JSON token, permissive decoder, or alternate instance-cache key remains.

## 11. WP2: Shared Execution Context and Engine Seams

### 11.1 `WorkflowExecutionContext`

Add the public runtime-only context specified in distributed-engine Section 16.5.
It owns:

- an optional validated/preallocated run ID,
- one thread-safe cancellation token,
- an idempotent terminal-finalization guard,
- the active execution binding needed to reject invalid finalization,
- the reserved `defer_success_finalization` state needed by Phase 1b without implementing a launcher.

Add keyword-only `run_context=` to `Workflow.compute()` and `Workflow.compute_steps()`.
When omitted, each public call creates a fresh clear context and ordinary run ID.
When supplied, a cancellation request made before startup remains visible.

The synthetic parent created for `compute(inputs=...)` and `compute_steps(inputs=...)` must reuse the exact same context.
It must not allocate a second run or clear cancellation.
Internal delegation with that same context is not an overlapping public execution and must not trip the active-execution guard.

One `Workflow` object permits one active public execution.
Overlap fails before compilation, run-view mutation, cache lookup, or engine startup.
`Workflow.cancel()` signals only the active context, and calling it while idle does not affect the next run.

Because a Python generator body is lazy, `compute_steps()` must attach or reserve its execution context at the public call boundary rather than waiting until the first `next()`.
Closing an unexhausted iterator must detach the context and apply the engine's configured cleanup after draining submitted work.

Phase 1a uses normal success finalization but implements and unit-tests the complete context finalization state and `defer_success_finalization` behavior specified in distributed-engine Section 16.5.
Launcher return persistence and the act of selecting deferred success in a submitted run remain Phase 1b.

### 11.2 Backend-neutral lifecycle

Refactor shared engine lifecycle validation, resource acquisition, execution cleanup, close, and context management around `ResourceLifetime` and backend-neutral hooks.

Direct execution accepts only its no-resource default policy.
Wetlands applies the final manager ownership and reuse behavior.
Parsl maps the same values to DFK/executor ownership described in Section 13.5 of this guide.

Remove transitional engine-factory and import-re-export paths while migrating these backends.
Shared modules import focused owners directly; if `engine/common.py` remains, it contains only intentionally shared values and helpers and is not an alternate engine namespace.

`close()` is idempotent.
Executing a closed engine raises `RuntimeError`.
Exhausting or explicitly closing a steps generator applies its execution cleanup policy.

### 11.3 Shared hooks

Add narrow, tested hooks for:

1. compiled-execution startup and preflight,
2. processing-node preparation used by `NodeStep.prepare()`,
3. immutable processing dispatch,
4. submitted-work cancellation and drain,
5. execution cleanup and engine close,
6. scheduler-level failure collection and deterministic primary selection.

The immutable dispatch request carries the tool, compiled/scoped node identity, workflow and run context, reusable attempt or transient invocation identity, resolved arguments, aligned positions and index labels, row and batch contexts, batch override state, and resource requirement.
It must contain everything a backend needs without reading mutable workflow construction state.

Cache hits do not warm a worker environment or acquire a DFK.
Parsl preparation never calls Wetlands, and direct preparation acquires no remote resource.

### 11.4 Deterministic compiled order

Compilation assigns every real tool a stable ordinal from deterministic topological order, using scoped node path as the ready-node tie breaker.
The scheduler retains independent-node failures until submitted work has drained.

The canonical failure key is:

```text
(compiled node ordinal, first input position, task id)
```

Whole-node batch position is `-1`.
The smallest observed key is the public primary failure regardless of completion race.
Additional failures may be attached as structured diagnostics.

The shared scheduler must retain independent-branch concurrency.
Do not make the whole workflow sequential merely to simplify Parsl collection.

## 12. WP3: Shared Cache Identity, Storage, and Publication

### 12.1 Recursive provenance recipes

The compiler records a provenance recipe for every consumed workflow-boundary value.
The recipe identifies the real published provider, stable workflow output selector, provider scoped key, and dependencies needed to resolve its selected result key and record ID after execution.
For a binding that consumes the whole assembled boundary dataframe, the recipe also includes the ordered stable output-port IDs and current public output-name mapping because a rename changes assembled column labels and whole-dataframe identity.

The runtime resolver recursively substitutes selected real-provider references.
It never substitutes a workflow-boundary diagnostic signature.
Completion-only terminal dependencies gate boundary success but do not enter consumer cache identity.

The resolver returns `str | None`.
It returns `None` if any consumed published value lacks a selected immutable record.
That `None` propagates to downstream consumers unless a future explicit materialization contract creates a selected record.

Planning retains unresolved recipes as `PENDING_UPSTREAM` without acquiring resources or fabricating a key.
Workflow aggregate plan entries never own a final result key or selected record ID.

### 12.2 Reusable and non-reusable execution

For a reusable result key, use the canonical attempt, immutable record, guarded current selection, run/latest view, and optional output-view flow.

For `result_key is None`:

- perform no reusable lookup,
- create no result-key attempt, record, `current.json`, conflict, latest pointer, run-node record pointer, or output projection,
- omit result key and record ID from progress,
- execute real tool code for current in-run consumers,
- use only the contract-backed run-scoped transient context for a `ProcessingTool`,
- return a local in-memory result for a `DataFrameTool`,
- retain transient owned outputs for the contract-defined attached-run lifetime.

### 12.3 Canonical dataframe identity

Use one shared canonical logical dataframe digest helper for:

- published `DataFrameTool` results,
- published `ProcessingTool` results after path canonicalization,
- root dataframe input identity,
- any later submitted dataframe transport verification.

Include the normative index, ordered schema, scalar kind, Unicode, datetime, categorical, path-reference, and unsupported-object rules.
Do not include a run ID, attempt ID, runtime path, backend ID, worker metadata, or incidental Parquet bytes.

Write Parquet with the documented deterministic configuration.
If a file digest is retained for integrity, label and validate it separately from the logical dataframe digest used in content identity.

### 12.4 Publication and run provenance

Pass the active run ID into attempt diagnostics, guarded selection provenance, and run views.
Do not synthesize an attempt-derived run ID while a workflow execution is active.

Record the effective injected backend and scheduling policy in run metadata.
Do not report the workflow's stored preference when an explicit engine overrides it.

Publication remains orchestrator-owned and happens only after all required task results are accepted and validated.
It follows the complete canonical sequence in distributed-engine Section 14.5, including owned-path canonicalization, zero-row outputs, manifest validation, immutable installation, guarded first-valid selection, and loading the selected record.

If another valid record is already selected, downstream execution uses that selected record rather than the losing candidate.
Failed, cancelled, or abandoned attempts are never selected and are not deleted while a possible writer exists.

Persisted owned path cells are record-relative and cache loading rehydrates them beneath the exact selected record.
Reject absolute owned values, `..`, symlink escape, and resolved paths outside that record while preserving normalized declared external-path values.

Update `views/runs/<run-id>/nodes/<scoped-node>/` and `views/latest/` only for selected real-tool records, and update latest successful run only after the whole requested execution succeeds.
Automatic output-view materialization failure remains a warning, while explicit export remains strict.
Never put attempt, work, worker-local, output-view, run, executor, or launcher paths into result-key or record-ID material.

## 13. WP4: Public Parsl API and Lifecycle

### 13.1 Optional dependency

Add a `parsl` project extra to the orchestrator distribution and update the workspace lock.
The prepared Phase 1a dependency range is `parsl>=2026.5.25,<2026.6`.
Pin the development lock to a release inside that range and run the complete Parsl matrix against it.
Replacing this range requires an explicit WP0 dependency-validation review and a new bounded range; do not widen it opportunistically during implementation.
Parsl `2026.5.25` requires Python `>=3.10`, matching the orchestrator's supported floor while leaving the Python 3.9 core package independent of Parsl.

Ordinary `import bioimageflow`, non-Parsl workflow construction, graph/archive serialization, validation, and `Workflow.plan()` must not import Parsl.
Public Parsl value types and `ParslEngine` may be imported without Parsl installed as long as the optional library is loaded only when a Parsl operation requires it.

A missing dependency error names both `parsl` and the install target `bioimageflow[parsl]`.

Export all public Parsl configuration types, `ParslEngine`, `ParslTaskError`, `ResourceLifetime`, `WorkflowExecutionContext`, and `WorkflowCancelledError` through `bioimageflow.__all__`.
Export public core protocol/origin types explicitly if they are part of the supported worker API.

### 13.2 Canonical attached API

The canonical API is:

```python
engine = ParslEngine(
    parsl_config=config,
    executor_bindings=bindings,
    resource_lifetime="engine",
)

with engine:
    result = workflow.compute(inputs=inputs, engine=engine)
```

WP0 makes the following constructor the normative surface:

```python
ParslEngine(
    *,
    parsl_config=None,
    dfk=None,
    executor_bindings,
    node_routes=None,
    environment_routes=None,
    shared_runtime_root=None,
    execution="workflow",
    storage_mode="shared_fs",
    task_policy=None,
    resource_lifetime="execution",
)
```

Exactly one of `parsl_config` and `dfk` is required.
An injected DFK requires `resource_lifetime="external"`.
`"external"` without an injected DFK is invalid.
`storage_mode="staged"` is recognized but rejected as unavailable in Phase 1a.

The constructor validates and stores local configuration only.
It does not compile a workflow, touch storage, materialize an archive, start or attach a DFK, probe an executor, construct an app, or submit a task.

`execution="workflow"` follows the executed root workflow's policy.
Explicit `"parallel"` or `"sequential"` overrides it.
An explicitly injected engine has higher precedence than the workflow's stored engine preference.

### 13.3 Workflow factory

Add the exact keyword-only factory surface from distributed-engine Section 5.3:

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

Validate arguments by selected backend:

- direct rejects non-default lifetime, `env_manager`, and every Parsl argument,
- Wetlands applies its final lifecycle and manager behavior and rejects every Parsl argument,
- Parsl rejects `env_manager`, requires bindings plus Config/DFK, and forwards only Parsl runtime values.

The factory resolves its default `parsl_execution="workflow"` to the workflow's configured execution policy before constructing the engine.
An explicitly constructed reusable engine may retain `execution="workflow"` and resolve it separately for each executed root workflow.

Remove the private alternate engine-factory path and keep one final factory implementation.
Bare `Workflow(engine="parsl").compute(...)` has no implicit cluster configuration and fails before graph execution with an actionable construction example.

### 13.4 Public value validation

Implement strict, immutable, JSON-safe types for:

- `ResourceLifetime`,
- `ParslTaskPolicy`,
- `WorkerSlotCapacity`,
- `ExecutorCapabilities`,
- `WorkerEnvironmentAttestation`,
- `ExecutorBinding`.

Reject unknown capability, storage, resource, or origin values.
Reject booleans where an integer is required.
Normalize byte capacities and absolute paths once at the API boundary.
Keep live Config, DFK, executor objects, routes, credentials, and secrets runtime-only and out of graph/cache identity.

### 13.5 Lifecycle behavior

| Lifetime | End of execution or steps generator | `close()` | Ownership |
|---|---|---|---|
| `"execution"` | Drain, then clean the engine-created DFK and executors. | Idempotent cleanup. | One execution. |
| `"engine"` | Drain and retain the engine-created DFK. | Drain any active execution, then clean owned resources. | Engine session. |
| `"external"` | Drain only this engine's submitted futures; leave the DFK running. | Leave the caller's DFK and unrelated work untouched. | Caller. |

Bind apps to the explicit DFK.
Do not call global Parsl load/clear/cleanup functions.

One `ParslEngine` permits exactly one active `execute()` or `execute_steps()` call.
An overlapping call fails before run, cache, cancellation, DFK, or future state changes.
Independent branches inside that one execution remain concurrent.

## 14. WP5: Startup, Routing, Materialization, and Preflight

Startup has six ordered stages.
Do not acquire a DFK before stages one through four succeed.

### 14.1 Stage 1: compile and statically validate

Recursively compile real tools, local boundaries, published-provider recipes, scoped names, deterministic ordinals, and every enabled terminal completion gate.

Before processing submission, reject every detectable:

- partial or unresolved workflow,
- cycle, invalid binding, or invalid recursive boundary,
- unsupported storage mode,
- same-name environment with different dependency identity,
- unresolved worker origin,
- remote `ImageShared` input/output schema,
- known `SharedArray` value crossing a remote task boundary,
- Wetlands-only per-environment setting on a Parsl route,
- unsupported resource request.

Run canonical planning and identify known cache hits, misses, and `PENDING_UPSTREAM` nodes.
Conservatively validate routes and compatibility for every reachable executable `ProcessingTool` that may miss.

A run whose reachable real-tool work is completely and definitively cached may finish without DFK acquisition or executor preflight.
Do not classify a `PENDING_UPSTREAM` node as a known hit merely to obtain this fast path.
A cached shared-memory value may be returned locally when it never crosses a remote boundary, but a statically visible remote `ImageShared` route must fail before unnecessary cache rehydration.

### 14.2 Stage 2: resolve routes and capacities

Build one canonical worker requirement per reachable `ProcessingTool` from:

- environment name,
- canonical normalized dependency hash,
- `allow_flexible_versions`,
- compatible core requirement or worker API level,
- anchored local dependency paths,
- `ResourceSpec`.

Resolve executor label in this order:

1. explicit scoped-node route,
2. explicit canonical environment-identity route,
3. one unique compatible executor binding,
4. failure when zero or multiple bindings remain.

The binding mapping key must equal its contained label, and the label must exist in the supplied Config or DFK.
There is no implicit default executor, GPU executor, large-memory executor, or environment-name fallback.

Explicit routing selects a candidate label but never bypasses compatibility validation.
Every selected binding must attest the exact environment identity and compatible core requirement, include `shared_fs`, support the resolved tool-origin mode, and fit the node's resource request.

Validate every requested CPU, GPU, memory, and GPU-memory value against the attested homogeneous worker slot.
A slot CPU capacity and a requested CPU count are positive integers, GPU counts are non-negative integers, and memory strings use one canonical size parser before byte comparison.
A non-default request with no corresponding declared capacity fails.
Validate `ResourceSpec.max_concurrent` separately as a non-negative, non-boolean integer.
Zero retains the platform's unlimited default, and only a positive value constrains the BioImageFlow submission window; it is not a worker-slot capacity field.
Phase 1a does not pass a generic per-task Parsl resource dictionary.

### 14.3 Stage 3: validate preinstalled environments

Phase 1a supports only preinstalled executor environments.
The Parsl executor/provider configuration is responsible for starting workers in those environments.

Validate declared attestations and local dependency portability.
Do not call `WetlandsEnvManager.get_or_create()`, install packages, or start a nested Wetlands worker.

`WorkflowEnvironment.max_workers`, `worker_env`, and `worker_timeout` remain Wetlands-only fields and must be rejected for Parsl instead of reinterpreted.
Parsl capacity comes from executor configuration, `ParslTaskPolicy.max_in_flight`, and `ResourceSpec.max_concurrent`.

### 14.4 Stage 4: materialize archive sources

For every required `archive_module`, require a normalized absolute `shared_runtime_root`.
Verify the archive source ID and digest before materialization.

Install into a hash-addressed immutable directory under a concurrency guard.
Stage into a sibling directory, validate it completely, and install it atomically by rename.
Reject archive traversal, absolute members, duplicate paths, unsafe links, full/file digest mismatch, and an existing mismatched destination.
Preserve scoped module names and helper files required by the recursive archive loader.

Retain materialized sources while any active engine execution may import them.
Clean only unreferenced temporary installation directories under the documented retention policy.

### 14.5 Stage 5: acquire or attach the DFK

Create an owned DFK lazily from `parsl_config`, or attach to the injected external DFK.
Synchronize initialization so concurrent internal branch scheduling cannot create two kernels.

Reject a Config whose `retries` value is not zero.
For an external DFK, verify the effective retry behavior and selected executor labels without changing caller configuration.

Keep app construction bound to this explicit DFK and executor label.
Disable Parsl app caching so BioImageFlow v1 remains the only result cache.

### 14.6 Stage 6: run executor preflight

Submit at least one preflight app to every selected executor label before processing tasks.

The probe verifies:

- the app actually ran on the requested executor,
- compatible `bioimageflow-core` version and worker API,
- read/write/delete access to a unique attempt-shaped, non-cache sentinel under the shared storage root,
- the same absolute attempt/transient and source namespace seen by the orchestrator,
- every required origin can resolve without invoking tool processing code,
- installed distribution versions and source/archive hashes,
- observable interpreter and resource facts where safe.

The sentinel must not create a cache result, current pointer, run node result, or output view.
Clean it after every success or failure where cleanup is safe.

Preflight failures identify executor label, environment identity, capability, origin, and path/hash evidence.
They occur before a processing attempt is submitted.

## 15. WP6: One-Row Dispatch, Backpressure, and Collection

Start with `row_chunk_size=1` and implement the final bounded architecture immediately.
Do not build an unbounded-future prototype that later code must unwind.

`ParslTaskPolicy` defaults are:

```python
ParslTaskPolicy(row_chunk_size=1, max_in_flight=32)
```

Both values are integers greater than or equal to one, with booleans rejected.

The effective per-node in-flight limit is:

1. one under sequential execution,
2. otherwise the minimum of task-policy `max_in_flight` and a positive `ResourceSpec.max_concurrent`,
3. otherwise the task-policy limit.

Use a sliding submission window.
Register each future and its possible writer before exposing it to cancellation or completion handling.
Remove it from the active registry only after it is terminal and its result or exception has been observed.

All resolved path arguments and `ExecutionContext` paths are absolute and visible through the same path namespace on every selected executor.
`context.run_dir` remains the node attempt or transient workspace, not the workflow run-view directory.
Each row has a private `row_dir`, a whole-node batch has a private `batch_dir`, and `work_dir` is shared within the node execution.
Neither the engine nor a tool may use process-wide `os.chdir()`.

Immediately before submitting each task, inspect its fully resolved argument values and reject any runtime `SharedArray` that would cross the Parsl boundary.
This runtime check complements the earlier schema/route check and must run before the future or possible writer is registered as submitted.

Every source processing node uses the canonical single row at position zero with index string `"0"`.
Every non-source invocation carries its integer aligned position and original row-index string.

The orchestrator validates the exact result envelope before accepting any row.
It accumulates by integer position, verifies the original index, and builds the dataframe in input-position order rather than future-completion order.

Preserve output order inside one row.
Use the parent index for exactly one output, `parent::0`, `parent::1`, and so on for multiple outputs, and no row for an empty output list.
Return an empty dataframe with declared columns when the complete result is empty.

The one-row milestone exits only when a real Parsl run matches direct execution for dataframe values, result key, selected record behavior, and public progress identity.

## 16. WP7: Chunks, Batch, Empty Inputs, Validation, and Recursive Parity

### 16.1 Explicit row chunks

An explicit `row_chunk_size > 1` groups consecutive aligned positions.
Rows inside a chunk execute in increasing position and call `process_row()` exactly once each.
One row exception fails the complete task envelope.

Chunking must not change final dataframe order.
It also must not change cache identity, task retry policy, or scratch ownership.
There is no silent fallback from a failed chunk to individual rows.

### 16.2 Whole-node batch

If the tool class overrides `process_batch()`, submit the complete node as exactly one `process_batch` task.
Do not split it based on row chunk policy or executor capacity.

Accept the documented nested `list[list[Outputs]]` form and flat one-to-one `list[Outputs]` shorthand.
Require the nested outer length or flat length to match the invocation row count exactly.

Apply this cardinality rule and the same canonical output shape to direct and Wetlands as a shared prerequisite.

### 16.3 Empty and zero-row behavior

Implement the complete normative empty-input contract:

- skip an empty aligned batch by default,
- run an empty batch only for a real `process_batch()` override with `run_empty_batch=True` on a column-bound node,
- inspect `empty_batch_anchor_inputs` in declaration order,
- use the first usable non-empty anchor indexes sorted by `str(index)`,
- resolve other anchors by exact or parent-lineage match,
- fall back to the single synthetic index `"0"`,
- resolve arguments and templates in the orchestrator,
- publish existing resolved templated assets even when no dataframe row is returned,
- publish declared `zero_row_scalar_outputs` for the corresponding synthetic execution rows,
- never fabricate a sentinel dataframe row.

### 16.4 Shared output validator

The worker returns plain dictionaries.
The orchestrator reconstructs every dictionary against the tool's declared `Outputs` and produces one canonical `list[list[Outputs]]` shape for shared dataframe construction.

The validator must:

- distinguish the documented row and batch return forms,
- reject missing, extra, and undeclared fields,
- apply the library's lightweight annotation-based runtime checks,
- convert path values to worker-safe strings for transport and restore the canonical runtime form in the orchestrator,
- preserve declared field order,
- reject a runtime `SharedArray` returned across the remote boundary; `ImageShared` input/output schemas have already been rejected during static route validation,
- reject invalid owned, work, or worker-local paths before publication.

`IOModel` construction alone is not sufficient type validation.
Pydantic remains orchestrator-only.

### 16.5 Recursive and step parity

Verify that:

- internal `ProcessingTool` nodes use their stable scoped names and dispatch remotely,
- ready internal `DataFrameTool` nodes execute under orchestrator coordination on the main thread and boundaries remain local,
- every enabled internal terminal, including a detached branch, gates boundary completion,
- boundary output assembly uses stable ports, final public names, compatible published indexes, and canonical index lineage,
- completion-only changes do not invalidate consumers of unchanged published values,
- zero-output workflows run their terminals and return the canonical empty dataframe,
- reserved `::` source indexes are rejected and unrelated roots or divergent sibling explosions raise the canonical alignment errors,
- a disabled ordinary real tool appears as a skipped step,
- a disabled `WorkflowNode` subtree is not expanded into steps,
- no boundary aggregate is yielded as a `NodeStep`,
- executing a skipped step raises `DisabledNodeError`, while advancing past an unexecuted non-skipped step applies the final auto-execution behavior,
- multiple requested targets omit skipped targets when another target executes, while all-skipped targets raise `DisabledNodeError`,
- parallel policy overlaps independent processing nodes,
- sequential policy permits one real node and one row future at a time.

Node-finalized execution remains the rule.
Do not stream partial upstream rows into downstream nodes.

## 17. WP8: Progress, Errors, Cancellation, Drain, and Cleanup

### 17.1 Progress

Emit only the final statuses:

- `started`,
- `row_progress`,
- `row_complete`,
- `completed`,
- `cached`,
- `failed`,
- `cancelled`.

Use scoped node names.
`row` is the zero-based aligned position, not a dataframe index label.
`started` includes the result key when it is already known.
`completed` and `cached` include the selected record ID for record-owning nodes.
Non-reusable nodes carry neither a result key nor a record ID.

For row and chunk execution, buffer `row_complete` and emit it once per accepted row in increasing aligned order.
When an out-of-order chunk closes a gap, emit every newly contiguous row in order.
Whole-node batch emits no `row_complete` events.

Serialize callbacks through one shared callback lock while allowing independent-node event streams to interleave.
Keep executor labels, task IDs, queued/running states, provider job IDs, and retries in backend diagnostics rather than new public statuses.

### 17.2 Errors

Add and export `ParslTaskError` as a subclass of `WorkerTaskError`.
It carries scoped node, complete tool origin, executor label, task ID, execution attempt/invocation ID, retry number, row position or range, original exception type/message, and remote traceback when available.

Configuration, route, attestation, resource, origin, shared-path, core compatibility, shared-memory, partial-workflow, and opaque-retry failures occur before processing submission whenever detectable.

After task failure, stop new submission, request cancellation of unfinished futures, observe every terminal result/exception, and choose the deterministic workflow-wide primary failure.
No accepted subset becomes a record.

### 17.3 Cancellation

Cancellation is best effort at the worker boundary.
The orchestrator must:

1. establish the active execution context before compilation,
2. stop submitting new work after cancellation,
3. call `cancel()` on its unfinished futures,
4. retain and drain futures that cannot be cancelled,
5. ignore late successful results for construction and publication,
6. leave the incomplete attempt unselected,
7. preserve records selected by nodes completed before cancellation,
8. emit normalized cancelled progress and raise `WorkflowCancelledError`,
9. clean only resources owned by this engine.

A running `DataFrameTool` is observed for cancellation after its local call returns or raises.
Worker-side cooperative cancellation is not part of Phase 1a.

### 17.4 Future and writer drain

Every successful execution returns only after all futures it submitted are terminal.
Failure, cancellation, and `close()` request cancellation and drain every submitted future before engine reuse or owned cleanup.

Keep the future-to-attempt or future-to-transient-writer registry intact throughout drain.
Do not remove an attempt or transient directory while a task may still write.

`close()` during an active execution requests cancellation, waits without holding registry locks, then applies the configured lifecycle policy.
An external DFK remains untouched except for waiting on and cancelling this engine's own futures.

### 17.5 Retries and logs

Require effective Parsl retries to be zero and keep `task_retry=0` in every Phase 1a envelope.
Do not expose a retry-count or task-timeout option.

Do not install console handlers implicitly.
If task stdout/stderr is exposed, redirect it explicitly to unique diagnostic files outside immutable records and return references as backend metadata.
Logs never enter result-key or record identity.

## 18. Test Architecture

### 18.1 Test layers

Use four complementary layers:

1. Pure unit tests for strict public values, core envelopes/origins, canonical identity, route selection, and optional dependency behavior.
2. Fake DFK/AppFuture tests for ownership, overlap rejection, cancellation races, drain, close, cleanup, and deterministic failure selection.
3. Real Parsl `ThreadPoolExecutor` tests for fast attached control-plane, multi-label routing, bounded submission, progress, and cache integration.
4. At least one local process-isolated `HighThroughputExecutor` plus local provider smoke for serialization, fresh worker imports, origin isolation, persistent worker instances, and true shared-filesystem visibility.

The thread executor is useful but cannot prove process-boundary serialization or import isolation.
The process-isolated smoke is therefore a release gate, not optional confidence testing.

No external scheduler is required for the Phase 1a CI baseline.
Site configurations remain deployment tests through standard Parsl providers.

### 18.2 Markers and dependency isolation

Add a strict `parsl` marker for tests that execute the real optional runtime.
Document whether the fast thread-executor subset runs in the normal deterministic matrix or a dedicated Parsl job.
Mark only genuinely slower process/executor cases as `slow`; do not hide all Parsl semantics behind a slow opt-in tier.

Tests that prove `bioimageflow` imports without Parsl must run in an environment or subprocess where the extra cannot be imported.
Package artifact tests must verify the extra metadata without making Parsl a mandatory orchestrator dependency.
The development environment may install the extra for real-runtime tests, but that must not mask the missing-extra test; use an isolated base-wheel environment or an explicit import blocker for that case.

Update `tests/unit/test_core_package_metadata.py` deliberately because its current assertions forbid every project extra and Parsl in dependency groups.
Only the orchestrator distribution may expose the `parsl` extra, and the core distribution must remain NumPy-only.
Extend `tests/unit/test_package_artifacts.py` to inspect the built wheel metadata and verify that the base wheel imports without the extra.

### 18.3 Test ownership

Add a `parsl` area to `tests/ownership.toml`.
It owns `bioimageflow/parsl/` and the dedicated Parsl tests.
Add a separate `worker-protocol` area for `bioimageflow-core/bioimageflow_core/worker_protocol.py`, the final core worker entry point, `bioimageflow/worker_origins.py`, and their focused tests.
Extend the existing engine area for the complete `bioimageflow/backends.py` file and cross-engine tests, and extend the platform-foundation area for final public exports.
Each production path belongs to exactly one ownership area even when an area's edit tests overlap another area's integration gates.
Focused edit tests are organized into packages such as:

- `tests/unit/parsl/test_types.py`,
- `tests/unit/parsl/test_optional_dependency.py`,
- `tests/unit/parsl/test_engine_lifecycle.py`,
- `tests/unit/parsl/test_routing.py`,
- `tests/unit/parsl/test_preflight.py`,
- `tests/unit/worker_protocol/test_envelopes.py`,
- `tests/unit/worker_protocol/test_origins.py`.

Real-runtime coverage is split by concern, for example:

- `tests/integration/parsl/test_thread_executor.py`,
- `tests/integration/parsl/test_process_executor.py`,
- `tests/integration/parsl/test_recursive_workflows.py`,
- `tests/integration/parsl/test_cache_and_views.py`,
- `tests/integration/parsl/test_cancellation_and_failures.py`.

Every actual test module remains below 500 lines.
Shared Parsl fixtures belong in focused modules under `tests/testkit/` and remain below the testkit ceiling.

Extend existing coverage in:

- `tests/unit/test_environment_lifecycle.py`,
- `tests/unit/test_core_python39_compat.py`,
- `tests/unit/test_core_package_metadata.py`,
- `tests/unit/test_package_artifacts.py`,
- `tests/unit/storage/`,
- `tests/unit/test_cache.py`,
- `tests/unit/tool_loader/`,
- `tests/unit/test_workflow_clean_api.py`,
- `tests/integration/test_engine_injection.py`,
- `tests/integration/test_compute_steps.py`,
- `tests/integration/runtime_cache/`,
- `tests/integration/test_unified_workflows.py`,
- `tests/integration/test_batch_processing.py`,
- `tests/integration/test_progress_monitoring.py`,
- `tests/integration/test_shared_memory.py`,
- `tests/integration/test_resource_constraints.py`,
- `tests/integration/test_versioned_tools.py`.

### 18.4 Required race tests

Use controlled futures and barriers rather than timing-only sleeps to prove:

- out-of-order completion preserves dataframe and row-progress order,
- two sibling failures choose the same primary regardless of completion order,
- cancellation while the window is partially submitted stops later submission,
- a non-cancellable writer is drained before return and cleanup,
- `close()` racing an active execution cannot deadlock or clean early,
- overlapping use of one engine and one workflow is rejected before state mutation,
- independent engine instances can share an external DFK without touching each other's futures,
- concurrent first-valid publication uses the selected existing record downstream.

### 18.5 Acceptance traceability

Maintain a review table in the implementation pull request or permanent test documentation that maps every bullet in distributed-engine Sections 21.1 through 21.6 to a test.
Do not add that temporary review table to this repository unless it is maintained as project documentation.

Section 21.7 is explicitly Phase 1b and must remain unclaimed.

## 19. Milestone Exit Criteria

### 19.1 Shared-prerequisite gate

- The platform/storage contract changes are merged.
- Direct and Wetlands use the new execution context, lifecycle hooks, output validator, canonical digest, and deterministic failure path.
- Boundary signatures no longer substitute for selected-provider records.
- Optional cache identity and transient execution follow the storage contract.
- The deterministic non-Parsl tests pass without Parsl installed.

### 19.2 Control-plane gate

- Optional import and factory behavior are correct.
- Constructor side effects are absent.
- All lifecycle values, close/context manager, overlap rejection, and external ownership pass fake tests.
- Static route, capacity, shared-memory, retry, and origin validation fail before DFK acquisition where possible.
- Known fully cached execution completes without DFK acquisition.

### 19.3 Real-dispatch gate

- At least one successful preflight app runs per selected label in the required startup scope and proves placement/shared paths/core/origins.
- One-row, bounded-window, explicit chunk, and whole-batch execution pass on real Parsl.
- Thread and process-isolated test topologies both pass.
- Output, ordering, cache identity, publication, and progress match direct execution.

### 19.4 Semantics gate

- Recursive workflows and detached completion gates pass.
- Empty/zero-row, flat/nested batch, explosion, alignment, and output-validation behavior pass across engines.
- Shared-memory rejection is boundary-specific and does not break local cache behavior.
- Run/latest/output views and effective engine/run provenance match the canonical storage contract.

### 19.5 Failure and cleanup gate

- Remote errors retain complete task identity and traceback.
- Primary failure is independent of completion race.
- Success, failure, cancellation, early generator close, and active `close()` drain all submitted futures.
- No partial record is selected and no possible-writer attempt is removed.
- External DFK and unrelated futures remain caller-owned.
- Idle cancellation does not affect the next execution.

## 20. Validation Commands

During implementation, run the smallest focused tests for the work package first.
Use the repository ownership map after every coherent edit:

```bash
git diff --name-only | uv run python scripts/affected_tests.py --stdin
```

Before committing a work-package slice:

```bash
git diff --name-only | uv run python scripts/affected_tests.py --stdin --stage precommit
```

Before merging a child branch or closing a work-package gate:

```bash
git diff --name-only | uv run python scripts/affected_tests.py --stdin --stage merge
uv run python scripts/check_file_sizes.py
uv run python scripts/check_import_boundaries.py
uv run pytest tests/unit/test_development_workflow.py
```

The affected-test selector is advisory and never replaces the unconditional CI jobs.
Add every new source module to exactly one ownership area before its implementation commit is merged.

The final Phase 1a validation includes:

```bash
uv run ruff check .
uv run pytest
uv run pytest tests -m "not slow and not acceptance and not packaging and not package_tools and not complete and not wetlands and not public_data and not external_binary and not sairpico_binary and not model_runtime"
uv run pytest -m "parsl and not slow"
uv run pytest -m "parsl and slow"
uv run pytest -m "acceptance and not complete"
uv run pytest -m wetlands tests/integration/test_wetlands_smoke.py
uv run pytest tests/unit/test_package_artifacts.py
uv run sphinx-build -W --keep-going docs/source docs/_build/html
```

Run the repository's type-checking and package-artifact validation documented by the active README/CI configuration after the new public types and optional extra land.
Run the core compatibility matrix on Python 3.9 and the orchestrator matrix on every supported Python version.

If CI assigns the Parsl marker differently, update these commands, the root pytest marker definition, README, and testing reference in the same change.

## 21. WP10: Final GUI-Update Guide

WP10 starts only after WP9 passes and the final library commit set is stable.
No earlier work package writes a provisional GUI guide or changes library design to reduce GUI work.

Create `docs/parsl_phase1a_gui_update_guide.md` as a concise handoff for a separate GUI agent.
It describes only:

- final public engine construction and execution entry points relevant to a GUI,
- final progress, cancellation, error, and run-state surfaces,
- final storage, run-view, latest-view, output-view, manifest, and returned-path surfaces a GUI may consume,
- high-level categories of GUI code that must be reviewed or updated,
- a short verification checklist for the GUI agent.

The guide does not:

- document implementation internals,
- preserve a library interface for the GUI,
- provide compatibility adapters or migration code,
- reproduce the distributed-engine specification,
- include superseded architecture details,
- change the completed library.

Validate the guide against the final code and normative documentation, commit it as the last Phase 1a deliverable, and then close the implementation plan.

## 22. Phase 1b Handoff

Phase 1a is ready for Phase 1b only after every gate above passes for an attached run and the attached engine has no launcher assumptions.

Phase 1b may then reuse:

- runtime-only engine construction,
- `WorkflowExecutionContext` with preallocated run ID and deferred success,
- strict JSON-safe executor bindings,
- archive materialization and executor preflight,
- task/result/origin protocols,
- future draining and canonical cancellation,
- canonical cache, run-view, and public return inputs.

Phase 1a must not pre-create launcher namespaces, persist reconnectable state, serialize live Parsl objects, or add scheduler launcher adapters in anticipation of that work.
