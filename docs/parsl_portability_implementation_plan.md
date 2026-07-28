# Parsl Scheduler, Portability, and Remote DataFrameTool Implementation Plan

Status: planned.

Integration baseline: `d0ae49b` (`harden submitted Parsl execution lifecycle`).

## 1. Authority and Scope

This plan covers the next distributed-execution milestone defined by:

- Section 17.10, launcher backends;
- Section 18, no-shared-filesystem execution;
- Section 19.1, remote `DataFrameTool`.

Section 19.2, streaming, is explicitly excluded.
No work package may add partial DataFrame publication, downstream consumption before node finalization, streaming accumulators, or a streaming-facing public API.

The normative behavior remains owned by:

- `docs/source/specs.md`;
- `docs/source/reference/unified_workflow_contract.md`;
- `docs/source/reference/output_cache_storage.md`;
- `docs/source/reference/parsl.rst`;
- `docs/parsl_distributed_engine_specs.md`.

WP0 updates those contracts before implementation.
If this plan and a normative contract disagree after WP0, the normative contract wins and the plan must be updated before downstream work continues.

The implementation targets one clean final architecture.
It does not add compatibility readers, deprecated aliases, dual schemas, fallback execution paths, or migration support for development cache and launcher data.
When a wire or storage schema changes, writers, readers, tests, fixtures, and documentation move to the new schema together and the superseded implementation is removed.

## 2. Assessment: The Three Targets Fit Together, but Prerequisites Are Missing

The proposed targets make sense as one program because scheduler-launched orchestrators and remote `DataFrameTool` both depend on portable artifact transport and reconnectable durable control state.
They must not be implemented as three unrelated features.

The current repository is ready to begin planning, but the existing specification is not yet sufficient to begin feature code.
WP0 must close these gaps:

1. Section 17.10 names scheduler backends but does not define scheduler configuration, job identity, submission recovery, state observation, cancellation, log collection, or the crash window between external submission and durable job receipt.
2. Section 18 defines an `ArtifactStager` for orchestrator-to-worker transfers, but also says the client and orchestrator may not share paths.
   Worker staging alone cannot satisfy reconnectable `WorkflowRun` control or transfer submission inputs when the client and orchestrator have no common filesystem.
3. The storage contract has only local absolute `external_path` identity.
   It does not define durable URI normalization, immutable version identity, credential exclusion, or when a URI makes a result non-reusable.
4. `ProcessingTaskV1`, `ExecutionContext`, and worker tool origins carry orchestrator-visible absolute paths.
   They need a transport-neutral protocol before `storage_mode="staged"` can be enabled.
5. Worker tool origins currently load `ProcessingTool` from `bioimageflow-core`.
   Remote `DataFrameTool` needs a full-runtime worker origin, a pandas/Arrow transport, an environment attestation, and a separate strict task/result envelope.
6. `DataFrameTool` has no remote opt-in, resource/environment declaration, whole-frame execution contract, or tool-facing asset context.
7. The launcher state machine has no scheduler-submission or queued-job states.
   Reusing `prepared` or `starting` would make receipt-loss recovery, cancellation, and job disappearance ambiguous.
8. CI has no deterministic scheduler simulator, isolated staged-filesystem harness, remote run-store conformance suite, or remote-DataFrame worker fixture.

These are prerequisites inside this plan.
No additional product decision is required before WP0 if the final-design choices below are accepted.
Access to real Slurm, PBS, LSF, and OAR sites is useful only for environment-dependent acceptance near the end; deterministic CI must not depend on those sites.

## 3. Final Outcome and Definition of Done

This milestone is complete only when all of the following are true:

- `slurm`, `pbs`, `lsf`, and `oar` are real scheduler launcher backends rather than reserved error values.
- A scheduler-launched run persists a submission intent, an exact external job identity, scheduler observations, cancellation intent, logs, and recovery evidence without persisting credentials.
- Crash recovery cannot submit a duplicate scheduler job or orphan an untracked job silently.
- `submitting` and `queued` distinguish an unresolved external submission from a durably identified scheduler job.
- `WorkflowRun.open()` can reconnect through a filesystem run store or the selected remote run store from only a run-store location and run ID.
- `storage_mode="staged"` runs `ProcessingTool` tasks on executors that cannot read orchestrator paths.
- Only schema-declared paths and artifacts are staged; arbitrary strings are never rewritten.
- Worker-local paths never enter result keys, cache records, run views, launcher returns, downstream canonical arguments, or progress payloads.
- Installed tools and transferred custom-source bundles both work without a shared import path.
- A `DataFrameTool` can explicitly opt into one whole-frame remote invocation.
- Remote `DataFrameTool.merge_dataframes()` and `transform()` execute together on one worker over verified DataFrame transports.
- Dynamic, passthrough, renamed, path-bearing, empty, multi-index, and source `DataFrameTool` results match local semantics.
- Cache identity and immutable-record publication remain orchestrator-owned and transport-independent.
- Shared-filesystem and staged execution produce the same result key and record ID for equivalent logical inputs and outputs.
- Cancellation, deterministic failure selection, future draining, claim recovery, and finalization remain correct across scheduler, staging, and remote-DataFrame paths.
- All new public values are strict, JSON-safe where persisted, lazily imported with optional dependencies, and represented in the final documentation.
- Streaming remains absent.
- The complete focused, integration, acceptance, documentation, type, lint, file-size, and import-boundary gates pass.
- A final GUI integration guide describes the new launcher states, run-store locations, remote result materialization, artifact references, and configuration surfaces at a high level.

## 4. Included and Excluded Work

### 4.1 Included

- Scheduler launcher adapters for Slurm, PBS, LSF, and OAR.
- A scheduler-adapter factory reference with JSON-safe arguments and opaque secret references.
- Durable scheduler intent, job receipt, query observations, cancellation, and recovery.
- New scheduler-submitting and scheduler-queued launcher states with exact legal transitions.
- A backend-neutral run-store protocol for launcher control and submitted public returns.
- Schema-aware client-to-orchestrator packaging of workflow archives, root DataFrames, and declared input files and trees through the run store.
- The current filesystem run store as one implementation.
- One production remote run-store implementation with conditional-write semantics.
  The selected implementation is S3-compatible object storage behind an optional dependency, with a deterministic conformance fake and an optional real-service acceptance test.
- An explicit submitted execution storage path on the orchestrator host when the run store is not the workflow's filesystem storage.
- Durable URI reference identity and validation.
- A backend-neutral artifact staging contract and a Parsl adapter.
- Staged file, directory-tree, DataFrame, tool-bundle, and declared-output transfer.
- A new strict worker task/result protocol that contains logical artifact references rather than foreign absolute paths.
- Full-runtime worker attestation for remote `DataFrameTool`.
- Remote whole-frame `DataFrameTool` execution, including source and merge tools.
- Optional tool-facing context for remote `DataFrameTool` owned assets and scratch.
- Focused test ownership, scheduler simulators, run-store conformance fixtures, and isolated staging fixtures.
- Current reference documentation, acceptance traceability, and a final GUI handoff.

### 4.2 Excluded

- Streaming or partition pipelines.
- Partitioned `DataFrameTool`, map/reduce DataFrame execution, or automatic frame splitting.
- Direct submission of each BioImageFlow node or row as a scheduler job.
- Parsl provider configuration through the BioImageFlow launcher API.
- Package installation or environment creation inside worker tasks.
- Automatic selection of a scheduler, run store, stager, URI scheme, or credential source.
- Heuristic path detection in strings or object columns.
- Mutable external URI caching without an immutable version or digest.
- A general remote cache-browser service for clients.
  Submitted results remain available through `WorkflowRun`; canonical cache administration remains an orchestrator-side concern.
- Worker-originated publication into canonical records, current pointers, run views, or output views.
- Parsl app caching, opaque Parsl retries, or a second BioImageFlow result cache.
- Compatibility with launcher, worker, cache, or return artifacts written by the development implementation before this milestone.
- GUI implementation work.

## 5. Final Design Decisions to Freeze in WP0

### 5.1 Keep Three Independent Axes

Do not collapse these concepts into one `backend` field:

| Axis | Values | Responsibility |
|---|---|---|
| Orchestrator launcher | `local`, `manual`, `slurm`, `pbs`, `lsf`, `oar` | Starts, observes, and cancels the orchestrator process or job. |
| Run store | filesystem or remote store reference | Persists submission, status, claims, progress, logs, errors, scheduler identity, and public return data for client reconnection. |
| Executor storage mode | `shared_fs` or `staged` selected by each executor binding | Determines how task inputs, contexts, tool sources, DataFrames, and outputs cross the orchestrator-worker boundary. |

Scheduler launch does not imply staged worker execution.
Staged worker execution does not imply a scheduler-launched orchestrator.
A remote run store does not become the canonical result cache.

The supported combinations must be tested compositionally rather than inferred from one combined mode string.

### 5.2 Scheduler Launcher Configuration

Add a strict public factory reference:

```python
SchedulerLauncherRef(
    factory="my_app.cluster:build_slurm_launcher",
    kwargs={
        "partition": "analysis",
        "account": "imaging",
        "walltime": "02:00:00",
    },
    secret_refs={
        "credential": "BIF_SCHEDULER_CREDENTIAL",
    },
)
```

`OrchestratorLaunchConfig` gains a scheduler reference that is required for `slurm`, `pbs`, `lsf`, and `oar` and forbidden for `local` and `manual`.
The existing type is updated directly to the final schema.

The factory returns an object satisfying one internal `SchedulerLauncher` protocol:

```python
class SchedulerLauncher(Protocol):
    backend: Literal["slurm", "pbs", "lsf", "oar"]

    def submit(self, request: SchedulerSubmitRequest) -> SchedulerJobReceipt: ...
    def find_by_token(self, token: str) -> tuple[SchedulerJobReceipt, ...]: ...
    def status(self, job: SchedulerJobReceipt) -> SchedulerJobObservation: ...
    def cancel(self, job: SchedulerJobReceipt) -> SchedulerJobObservation: ...
    def collect_logs(self, job: SchedulerJobReceipt) -> SchedulerLogSnapshot: ...
```

The persisted request and receipt are strict versioned plain values.
Live Parsl provider objects are never launcher configuration.
Built-in adapters may share command-running helpers with Parsl provider concepts, but BioImageFlow does not depend on provider instances or their internal state.

Every scheduler command uses explicit argv or a generated immutable job script.
User values are validated and shell-quoted only by one focused renderer.
No literal credential, environment dump, callable, arbitrary shell fragment, or live scheduler object enters launcher JSON or job scripts.

### 5.3 Scheduler Submission Intent and At-Most-Once Recovery

The scheduler control tree adds:

```text
scheduler/
  intent.json
  job.json
  observations/
    <sequence>.json
  script
  script.digest.json
```

`intent.json` is installed before any external submission and contains a unique run-correlated submission token.
The adapter places that token in a scheduler-searchable job name, comment, or equivalent field.
`job.json` is an immutable receipt containing the canonical scheduler job ID and submission evidence.

Recovery rules:

1. If `job.json` exists, it is authoritative and no submission is repeated.
2. If only `intent.json` exists, recovery calls `find_by_token()`.
3. One exact matching job is adopted and persisted.
4. More than one matching live job is a protocol failure and must never be silently reduced to one.
5. Recovery searches both active and historical/accounting records through the adapter's documented visibility horizon.
6. No match never permits automatic resubmission unless the adapter supplies a verifiable idempotency guarantee or definitive evidence that the original request was rejected before job allocation.
7. An unresolved submission remains `submitting`, surfaces a stable submission-uncertain diagnostic, and fails closed for execution and external cancellation until a matching job appears or the adapter proves absence.
8. A scheduler query outage leaves the run recoverable and non-terminal; it is not treated as absence.
9. A matching job may also self-report its scheduler identity from the orchestrator job environment, but the launcher verifies its token before installing the receipt.
10. A job that is confirmed absent after claim is recovered through the same finalization/lost rules as a disappeared local orchestrator.

### 5.4 Launcher State Changes

Add `submitting` and `queued` as first-class launcher states.
`submitting` means the immutable intent is durable and external submission may have occurred, but no exact scheduler receipt is durable yet.
`queued` means an external scheduler receipt is durable but the orchestrator has not acquired its execution claim.

The normative state graph gains:

```text
prepared -> submitting
submitting -> queued
submitting -> cancel_requested
submitting -> failed
submitting -> lost
queued -> starting
queued -> cancel_requested
queued -> failed
queued -> lost
cancel_requested -> cancelled | failed | lost
```

The scheduler adapter installs `intent.json` and commits `submitting` before invoking the external submit operation.
It commits `queued` only after `job.json` is durable.
The orchestrator validates the receipt and claims `queued -> starting`.

Cancellation is state-specific:

- cancelling `prepared` before external submission commits `cancelled`;
- cancelling `submitting` commits `cancel_requested`, continues receipt recovery, and cancels a matching job as soon as its identity is verified;
- cancelling `queued` commits `cancel_requested` before invoking scheduler cancellation;
- confirmed cancellation before orchestrator startup commits `cancelled` with no canonical run view;
- graceful cancellation after startup follows the existing workflow cancellation path;
- confirmed forced scheduler termination after startup commits `lost`;
- `finalizing` and terminal states remain non-cancellable.

Scheduler states are observations, not launcher states.
Each adapter maps its native queue states into a small internal observation enum without exposing scheduler-specific strings through `WorkflowRun.status`.

### 5.5 Run Store and Client-Orchestrator Separation

Introduce a strict `RunStoreRef` factory reference.
A path creates the built-in filesystem run store; a remote reference creates a store through an importable factory with JSON-safe arguments and opaque secret references.

The run store owns launcher/control data plus submission and public-return transport data.
The orchestrator's canonical cache, records, attempts, run views, and output views remain under its explicit execution `storage_path`.

For a remote run store, `submit_workflow()` requires an explicit `execution_storage_path` that is meaningful on the orchestrator host.
That path remains runtime launcher metadata and is not serialized into the workflow graph or archive.
For a filesystem run store colocated with the workflow, the selected final API may default `execution_storage_path` to `workflow.storage_path`.
The orchestrator materializes the workflow definition through the existing loader with that one explicit storage path.
This is a deployment-time runtime binding, not a serialized workflow setting, a fallback, or a second active storage root.
The canonical cache and view layout under the selected storage path does not change.

The run-store protocol must provide:

- immutable create and verified read;
- guarded compare-and-swap for mutable status;
- immutable per-sequence progress and scheduler observations;
- claim acquisition, heartbeat, expiry, and history;
- bounded log chunk upload and ordered read;
- content-digested file and directory upload/download;
- exact existence and metadata checks;
- prefix confinement under one run ID;
- deletion only through explicit operator retention;
- conditional requests that distinguish not-found, conflict, corruption, authentication failure, and temporary unavailability.

Remote stores must not emulate compare-and-swap with an unsafe read-then-write sequence.
The S3-compatible implementation uses conditional object writes and immutable sequence objects.

A schema-aware submission packager uses the common artifact codec to upload:

- the serialized graph or verified workflow archive;
- root DataFrame transports;
- path-declared files and directory trees referenced by invocation inputs, workflow constants, or DataFrame path cells;
- any other schema-declared submission artifact required before orchestrator execution.

The orchestrator materializes those artifacts under a confined per-run staging root before workflow loading and invocation reconstruction.
Client-local external paths remain logical input identities; their run-store objects and orchestrator materialization paths are transport copies and never replace that identity.
Arbitrary strings are not inspected or uploaded as paths.
Submission data remains available for the run's reconnect/recovery lifetime and is deleted only by explicit retention policy.

`WorkflowRun.open()` accepts the final run-store location and a run ID.
It does not need access to the orchestrator's canonical filesystem.
`WorkflowRun.result()` reads the self-contained return snapshot from the run store.
When an owned returned asset must become a local `Path`, the caller supplies an explicit result materialization root; the library does not return paths into a temporary directory with unclear lifetime.
`WorkflowRun.control_dir` and `WorkflowRun.view_dir` are removed because they cannot be truthful portable `Path` properties.
The handle exposes its non-secret run-store location and run ID; canonical view paths remain orchestrator-side diagnostics rather than client navigation APIs.

### 5.6 Artifact and URI References

Define one strict transport-neutral artifact reference with explicit kinds:

- `shared_path`: a canonical path proven visible at the same resolved location;
- `staged_file`: a content-verified regular file;
- `staged_tree`: a content-verified canonical directory tree;
- `dataframe`: canonical logical identity plus Parquet transport integrity;
- `tool_bundle`: a verified import bundle and origin identity;
- `external_uri`: a durable external reference not owned by a cache record.

Every reference carries a stable artifact ID and the metadata required by its kind.
Credentials, signed query parameters, worker-local paths, scheduler IDs, and temporary object-store keys are not identity material.

URI rules:

- scheme and authority are normalized explicitly;
- fragments are forbidden;
- credential-bearing authorities and secret query parameters are forbidden;
- scheme-specific normalization is implemented by a registered adapter rather than generic string rewriting;
- an immutable version ID or content digest is part of reusable identity;
- a mutable URI without immutable identity makes the consuming path non-reusable;
- transport copies and signed download URLs never replace the canonical URI in result-key material;
- local `file:` URIs follow the local path contract and do not masquerade as remote durable references.

`external_path` and `external_uri` remain distinct manifest and DataFrame logical kinds.

### 5.7 ArtifactStager Contract

Create an orchestrator-side `ArtifactStager` protocol selected per executor route.
It owns transfer planning, not graph scheduling or cache publication.

`ExecutorBinding` gains a required selected `storage_mode`.
Its `ExecutorCapabilities.storage_modes` remain the deployment attestation, and validation requires the selected mode to be attested.
An `artifact_stager` reference is required for `staged` and forbidden for `shared_fs`.
Remove the engine-wide `ParslEngine.storage_mode`; one execution may legitimately route nodes to shared and staged bindings.

Logical operations:

```python
class ArtifactStager(Protocol):
    def stage_inputs(self, request: StageInputRequest) -> StagedInputPlan: ...
    def stage_tool(self, request: StageToolRequest) -> StagedToolPlan: ...
    def prepare_worker_context(
        self,
        request: WorkerContextRequest,
    ) -> WorkerContextPlan: ...
    def collect_outputs(
        self,
        result: StagedTaskResult,
    ) -> CollectedTaskResult: ...
    def cleanup(self, task: StagedTaskIdentity) -> None: ...
```

The Parsl adapter may use Parsl `File`, `DataFuture`, and executor `storage_access` internally.
Those Parsl types do not enter BioImageFlow public values, cache identity, or worker protocol schemas.

Only fields declared path-like by `Inputs`, `Outputs`, output templates, DataFrame logical schemas, execution contexts, and tool origins are rewritten.
The stager walks typed values through one shared schema-aware codec.
It never guesses from string shape or filesystem existence.

Directory transport uses a deterministic archive plus canonical tree manifest.
Extraction rejects traversal, symlinks, special files, duplicate normalized names, and digest mismatch.

Worker scratch is allocated independently per task and is always worker-local.
It is cleaned only after task/result transfer is terminal.
Canonical attempt and transient cleanup remains governed by orchestrator-side writer lifetimes.

### 5.8 Worker Protocol Replacement

Replace `ProcessingTaskV1`, `ProcessingTaskResultV1`, and `WorkerToolOriginV1` with final transport-neutral processing schemas.
Do not retain a v1 decoder.

The task contains:

- task, invocation, optional cache-attempt, run, node, mode, retry, and route correlation;
- a tool origin with explicit `tool_kind`;
- typed scalar and artifact-reference arguments;
- logical row positions and indexes;
- a logical worker-context layout rather than orchestrator absolute directories;
- declared output artifact IDs and expected output schema.

The worker materializes artifacts and local `ExecutionContext` paths only after strict decode.
The result returns typed values and artifact IDs, never raw worker-owned absolute paths.
The orchestrator verifies and collects every referenced artifact before output reconstruction and publication.

Shared-filesystem execution uses the same protocol with `shared_path` references.
There is one processing-worker protocol and one output validator for shared and staged processing, not separate shared and staged result formats.
Remote `DataFrameTool` uses a separate full-runtime task/result envelope that reuses the common artifact-reference, origin, correlation, and integrity values.
The separation prevents pandas and PyArrow from entering `bioimageflow-core`.

### 5.9 Tool Origins Without Shared Paths

The new origin schema has an explicit `tool_kind` of `processing` or `dataframe`.

Supported staged behavior:

- `installed_module` resolves from exact installed distribution metadata without transfer;
- custom source, source-file, shared-module, archive-module, and versioned-module code is normalized into one verified transferable bundle;
- bundle identity is content-based and independent of worker extraction path;
- helpers, packages, and relative imports are included exactly once;
- the worker caches loaded tool instances by complete origin identity and tool kind;
- preflight proves either exact installed resolution or successful bundle transfer and import.

No worker installs packages.
No tool origin contains a client, orchestrator, or worker-local extraction path as identity.

### 5.10 Remote DataFrameTool Contract

Remote execution is opt-in on the tool class:

```python
class MyRemoteTable(DataFrameTool):
    worker_execution = "whole_frame"
    environment = ...
    resources = ...
```

The default remains `worker_execution="disabled"`.
Current tools therefore remain local unless deliberately changed.

`whole_frame` means:

1. the orchestrator resolves constants, defaults, routes, environment identity, resource requirements, expected schema, and cache identity;
2. every upstream DataFrame is written as a verified canonical DataFrame transport;
3. one worker loads all input frames;
4. `merge_dataframes()` and `transform()` execute together on that worker;
5. the worker writes one verified result DataFrame transport and declared owned assets;
6. the orchestrator validates the result, collects assets, publishes through the existing immutable-record path, and emits terminal progress.

There is no partitioning and no split merge/transform execution.

Direct and Wetlands engines may execute an opted-in tool locally.
Parsl executes it remotely when the selected engine and route support the declared worker mode.
Once Parsl selects remote execution, route, preflight, staging, or worker failure is reported; it does not fall back locally.

Dynamic schemas remain pure orchestrator-side planning behavior through `resolve_outputs()` and `resolve_merge_schema()`.
The remote result must satisfy the resolved schema, passthrough contract, index rules, and path kinds.
An unresolved schema that is legal locally remains legal only if the returned strict result supplies enough schema evidence for the shared validator.

Remote `DataFrameTool` workers require the full `bioimageflow` distribution, pandas, PyArrow, and the selected tool package.
Executor attestations gain a runtime profile that distinguishes core processing workers from full DataFrame workers.
Preflight checks the full worker API and exact supported dependency requirements.

`transform()` may accept an optional keyword-only DataFrame execution context.
That context exposes only worker-local `assets_dir` and `work_dir` plus logical run/node identity.
Owned asset paths returned through declared path columns are collected and rewritten before publication.
Undeclared files and worker-local paths are rejected.

### 5.11 Cache, Return, and Progress Ownership

The orchestrator remains the only writer of:

- result keys;
- cache attempts and terminal attempt metadata;
- immutable cache records;
- current selection and conflict reports;
- canonical run and node views;
- output views;
- submitted public return manifests;
- public progress ordering.

Artifact transport digest, transfer URI, worker path, scheduler job ID, run-store object key, and staging duration do not enter result keys or record IDs.

Remote `DataFrameTool` cache identity includes its declared environment identity and complete tool origin.
This identity applies consistently when the same opted-in tool executes locally under direct/Wetlands and remotely under Parsl.

## 6. Expected Public Surface

The exact names are frozen in WP0, but the intended usage is:

```python
run = submit_workflow(
    workflow,
    parsl_config=ParslConfigRef(
        "my_app.parsl:build_config",
        {"profile": "cluster"},
    ),
    executor_bindings=bindings,
    run_store=RunStoreRef(
        "bioimageflow.runstores.s3:build",
        {
            "bucket": "bioimageflow-runs",
            "prefix": "production",
            "endpoint": "https://objects.example.org",
        },
        secret_refs={
            "credential": "BIF_OBJECT_STORE_CREDENTIAL",
        },
    ),
    execution_storage_path="/cluster/scratch/bioimageflow/results",
    launch=OrchestratorLaunchConfig(
        backend="slurm",
        scheduler=SchedulerLauncherRef(
            "bioimageflow.launcher.schedulers.slurm:build",
            {
                "partition": "analysis",
                "account": "imaging",
                "walltime": "02:00:00",
            },
        ),
    ),
)
```

Executor staging is explicit:

```python
binding = ExecutorBinding(
    label="remote-workers",
    environments=environments,
    capabilities=capabilities,
    storage_mode="staged",
    artifact_stager=ArtifactStagerRef(
        "my_app.staging:build_stager",
        {"profile": "cluster-transfer"},
        secret_refs={
            "credential": "BIF_TRANSFER_CREDENTIAL",
        },
    ),
)
```

Reconnection does not require the orchestrator filesystem:

```python
run = WorkflowRun.open(run_store, run_id)
run.refresh()
result = run.result(materialization_path=project_results)
```

These are final APIs, not compatibility wrappers around alternate old forms.
WP0 may refine names and field placement to respect package boundaries, but it must preserve the separation of launcher, run store, execution storage, and executor staging.

## 7. Package and Responsibility Boundaries

Expected ownership:

| Area | Responsibility |
|---|---|
| `bioimageflow/launcher/` | Common submitting/queued states, scheduler intent/receipt/observation schemas, recovery orchestration, and backend dispatch. |
| `bioimageflow/launcher/schedulers/` | Scheduler protocol, safe script rendering, command runner, native state mapping, and focused Slurm/PBS/LSF/OAR adapters. |
| `bioimageflow/runstore/` | Filesystem and remote run-store protocol, conditional operations, submission/return content transfer, and conformance helpers. |
| `bioimageflow/staging/` | Artifact/URI values, schema-aware path traversal, transfer plans, directory bundling, and backend-neutral stager protocol. |
| `bioimageflow/parsl/` | Parsl-specific staging adapter, full-runtime routes/preflight, processing and DataFrame app submission, future drain, and diagnostics. |
| `bioimageflow/backends.py` and `bioimageflow/engine/` | Scheduler-owned node semantics and separate processing/DataFrame dispatch seams; no scheduler CLI or object-store code. |
| `bioimageflow/storage/` and `bioimageflow/cache/` | Durable URI identity rules, canonical publication, record validation, and local canonical storage; no launcher or Parsl imports. |
| `bioimageflow/dataframe_tool.py` | Final remote opt-in metadata and optional tool-facing context signature. |
| `bioimageflow/dataframe_worker/` | Full-runtime strict remote DataFrame task/result codec and worker entry point. |
| `bioimageflow-core` | Python 3.9-safe processing worker protocol, typed artifact references needed by processing tasks, and origin loading that does not import pandas. |
| `tests/testkit/` | Fake schedulers, run-store conformance fixtures, isolated staging roots, deterministic transfer faults, and reusable remote tools. |
| `tests/unit/launcher/`, `tests/unit/staging/`, `tests/unit/runstore/`, and `tests/unit/parsl/` | Focused contracts and fault injection. |
| `tests/integration/parsl/` | Real Parsl shared/staged/remote-DataFrame compositions. |

The enforced storage → cache → engine → workflow dependency direction remains.
Storage and cache do not import launcher, run-store, staging backend implementations, Parsl, or external scheduler/object-store clients.
`bioimageflow-core` remains free of pandas, PyArrow, Pydantic, Parsl, scheduler clients, and object-store clients at module import time.

Optional external packages are loaded only after a caller selects the corresponding run store, scheduler, stager, or Parsl path.

## 8. Worktrees, Agents, and Plan-Update Cadence

This plan is too large for a safe one-pass implementation.
It must be executed through an integration worktree and phase checkpoints.

Create:

```text
.worktrees/parsl-portability
branch: feature/parsl-portability
base: the commit containing this approved plan
```

Use short-lived child worktrees only after their input contracts are frozen.
Good parallel slices are:

- scheduler simulator plus scheduler adapter implementations after WP0;
- artifact-reference/worker-protocol work and run-store conformance work after WP0;
- focused staging tests and remote-DataFrame worker codec after WP1.

Do not develop the shared launcher state machine, scheduler recovery, and run-store CAS logic concurrently in overlapping files.
Do not start remote `DataFrameTool` engine integration until the artifact and worker protocols pass their exit gates.

Use at most two writing agents plus the integration agent.
Reserve the remaining slot for read-only contract review, crash/race analysis, or acceptance audit.

The integration agent owns:

- the master plan;
- normative specs;
- public façades and exports;
- shared launcher state;
- dependency locks;
- `tests/ownership.toml`;
- merge order;
- cross-feature tests;
- final documentation and GUI handoff.

Update this plan:

- after every work-package exit gate;
- before creating child worktrees from a new baseline;
- after merging parallel work and before starting its consumers;
- immediately when a public, wire, storage, identity, or state-machine decision changes;
- after a real scheduler or object-store observation disproves a simulated assumption.

Do not update the plan after every file or test.
Each checkpoint records commits, focused validation, unresolved risks, and any changed downstream dependency.

After a child branch is merged, move its worktree to Trash, prune worktree metadata, and delete the merged branch.

## 9. Delivery Sequence

| Work package | Deliverable | Depends on | Exit gate |
|---|---|---|---|
| WP0 | Normative contracts and exact public/state/wire/storage decisions | Current baseline | Sections 17.10, 18, 19.1, storage reference, public specs, and acceptance list are complete and mutually consistent. |
| WP1 | Test infrastructure and strict common protocol values | WP0 | Scheduler simulator, run-store conformance fake, staged roots, artifact refs, URI validation, and worker codec round trips pass without Parsl startup. |
| WP2 | Queued launcher state and scheduler adapter foundation | WP0, WP1 | Intent/receipt/CAS/recovery/cancellation fault tests pass against the simulator. |
| WP3 | Slurm, PBS, LSF, and OAR launchers | WP2 | Every adapter passes one shared conformance suite plus backend-specific parsing and state tests. |
| WP4 | Filesystem and S3-compatible run stores | WP0, WP1 | Conditional state/claim/progress/log/return operations pass conformance and process-race tests. |
| WP5 | Transport-neutral processing protocol and staged `ProcessingTool` | WP1 | A real Parsl process executor runs with no canonical path in task/result payloads and publishes the same immutable result as shared mode. |
| WP6 | Tool-bundle transfer, staged preflight, directories, and failure cleanup | WP5 | Installed and custom tools, files, trees, cancellation, corruption, and cleanup pass in isolated roots. |
| WP7 | Full-runtime attestation and remote whole-frame `DataFrameTool` | WP1, WP5, WP6 | Source, transform, merge, dynamic schema, index, paths/assets, cache, failure, and cancellation tests match local behavior. |
| WP8 | Client-orchestrator separation and scheduler/run-store integration | WP2, WP3, WP4 | A scheduler-simulated run submits, queues, executes, reconnects, cancels, logs, and returns without shared client/orchestrator paths. |
| WP9 | Cross-feature compositions and crash-boundary hardening | WP3 through WP8 | Scheduler + remote run store + staged ProcessingTool + remote DataFrameTool compositions pass all injected crash boundaries. |
| WP10 | Acceptance, packaging, optional dependencies, and performance guardrails | WP9 | Full repository validation and every new acceptance item pass; focused loops remain within documented limits. |
| WP11 | Final normative docs and GUI integration handoff | WP10 | Published docs describe only the final architecture and the concise GUI guide is complete. |

WP3 and WP4 may proceed in parallel after their shared schemas are frozen.
WP5 protocol work may proceed in parallel with scheduler adapter implementation.
WP7, WP8, and WP9 are integration-heavy and should be merged sequentially.

## 10. Work-Package Details

### WP0 — Contract Closure

Update the normative documents before feature implementation.

Required decisions:

- final scheduler reference and adapter protocol;
- generated script and scheduler option safety;
- exact submitting and queued state transitions;
- submission-token recovery and scheduler uncertainty windows;
- scheduler job, observation, and log schemas;
- run-store interface and S3 conditional-write behavior;
- explicit remote submitted execution storage;
- schema-aware client-to-orchestrator submission packaging and materialization;
- artifact/URI reference schemas and identity;
- processing worker protocol replacement;
- tool-origin `tool_kind`;
- per-executor stager placement;
- removal of the engine-wide storage-mode selection in favor of explicit binding selection;
- remote DataFrame opt-in, environment/resource metadata, context, and task/result schema;
- returned remote asset materialization;
- removal of filesystem-specific `WorkflowRun.control_dir` and `WorkflowRun.view_dir`;
- cache identity and non-reusable URI rules;
- acceptance items and environment-dependent site tests.

Exit checks:

- no ambiguous client/orchestrator versus orchestrator/worker use of “no shared filesystem” remains;
- every new mutable operation has an owner, atomicity rule, recovery rule, and terminal behavior;
- every path or URI has an identity rule and a transport-integrity rule;
- no scheduler-native state leaks into public launcher state;
- streaming remains explicitly excluded.

### WP1 — Testkit and Common Protocol Foundation

Build tests before production adapters:

- a fake scheduler executable/service with submit, delayed visibility, query outage, queued, running, completed, failed, cancelled, duplicate-token, malformed-output, and disappeared-job states;
- a run-store conformance harness with conditional conflicts, stale reads, unavailable reads, truncated transfers, and concurrent writers;
- isolated client, orchestrator, and worker roots that fail if a foreign absolute path crosses either boundary;
- deterministic file, tree, DataFrame, and bundle fixtures;
- transfer corruption and cancellation hooks;
- remote DataFrame test tools with static, dynamic, passthrough, source, merge, asset, empty, multi-index, and failure behavior.

Implement strict pure values and codecs for artifact references, URIs, scheduler requests/receipts/observations, and worker task correlation.

### WP2 — Scheduler Control Plane

Add submitting/queued states and shared scheduler lifecycle logic independently of a native backend:

- allocate and validate scheduler intent;
- commit submitting before the external side effect;
- render one immutable command/script input;
- submit through a protocol adapter;
- install job receipt before queued status;
- recover intent-without-receipt;
- search active and historical scheduler state through a declared visibility horizon;
- refuse unsafe automatic resubmission after an ambiguous submit;
- accept a verified self-reported job identity from an orchestrator job;
- query and append observations;
- claim queued startup;
- cancel queued/running jobs;
- reconcile scheduler completion or disappearance;
- collect and redact logs;
- preserve exact terminal error/status correlation;
- keep cancellation and success finalization linearizable.

Run dedicated race review for:

- cancellation versus scheduler receipt;
- submission intent versus submitting status;
- ambiguous submit return versus active and accounting visibility;
- scheduler receipt versus queued status;
- orchestrator claim versus queued cancellation;
- scheduler disappearance versus finalizing;
- duplicate recovery controllers;
- query outage versus absence;
- status/error/log partial writes.

### WP3 — Native Scheduler Adapters

Implement one focused module per scheduler with the shared command runner and renderer.

Each adapter defines:

- job-name/token limits and canonical encoding;
- safe directive vocabulary;
- submit argv and exact receipt parser;
- token lookup;
- status query and native-to-internal mapping;
- cancel argv and confirmation behavior;
- stdout/stderr discovery;
- timeout and command-error normalization.

Do not expose unrestricted scheduler directives as arbitrary shell text.
Site-specific behavior belongs in an importable custom adapter factory.

Real-site tests are marked and opt-in.
The fake scheduler remains the required CI gate.

### WP4 — Run Store

Refactor launcher repository/control operations behind the run-store protocol without changing canonical cache ownership.

Implement:

- filesystem store using the current confinement and locking rules;
- S3-compatible store using immutable keys, ETags/version tokens, conditional writes, and per-sequence immutable events;
- content-addressed uploads for inputs, logs, and returns;
- schema-aware submission package installation and confined orchestrator materialization;
- bounded retry only for explicitly temporary store errors;
- no retry on schema, authentication, conflict, or corruption errors;
- store factory and opaque credential resolution;
- `WorkflowRun.open`, polling, cancellation, logs, progress, and result over both stores;
- explicit local result materialization.

The filesystem and S3 stores must pass the same behavioral conformance tests.
Storage-specific representations may differ, but public state and result behavior may not.

### WP5 — Staged ProcessingTool

Replace the processing worker protocol and migrate direct remote backends together.

Implementation flow:

1. Resolve canonical input values and output declarations in the orchestrator.
2. Convert declared paths and contexts into logical artifact references.
3. Stage artifacts and tool origin for the selected executor.
4. Build worker-local directories only inside the worker entry point.
5. Execute the existing processing method.
6. Encode results with artifact IDs rather than local paths.
7. Collect and verify outputs.
8. Reconstruct canonical values.
9. Publish through the existing cache/transient path.
10. Clean worker scratch only after transfer and future termination.

Migrate Wetlands if it consumes the common processing protocol.
Do not retain different protocol versions for Wetlands and Parsl.

### WP6 — Staged Origins, Trees, Preflight, and Cleanup

Complete:

- portable bundle normalization for non-installed origins;
- deterministic archive and tree manifests;
- full transfer digest verification;
- worker import and origin identity;
- staged executor preflight;
- staged anchored dependency handling;
- file and directory inputs;
- file and directory outputs;
- zero-row declared assets;
- transient invocation assets;
- task failure before and after output creation;
- cancellation with non-cancellable transfer;
- explicit cleanup eligibility.

Preflight must fail before processing when an executor cannot support the selected stager, runtime, URI scheme, origin, or resource request.

### WP7 — Remote DataFrameTool

Add a separate DataFrame dispatch seam and Parsl adapter while leaving graph scheduling and publication in the shared engine.

Implement:

- opt-in metadata and validation serialization;
- environment and resource requirement compilation;
- full-runtime routing and preflight;
- input-frame transport and logical digest verification;
- one whole-frame app per node;
- worker origin loading by `tool_kind="dataframe"`;
- combined merge and transform;
- optional context and owned assets;
- strict result frame, schema, index, and artifact validation;
- orchestrator-side cache/transient publication;
- node-level progress and diagnostics;
- cancellation and complete future draining;
- deterministic remote exception normalization.

No built-in tool is switched to remote execution merely to exercise the feature.
Dedicated fixtures prove the contract first; individual tool packages may opt in later through their own reviewed changes.

### WP8 — Remote Submitted Runs

Compose scheduler launch and run store:

- client uploads submission and root DataFrames to the run store;
- client packages every declared local file/tree input through the same typed artifact codec;
- scheduler script contains only run-store location, run ID, safe adapter metadata, and opaque credential references;
- orchestrator resolves its explicit execution storage path;
- orchestrator materializes the graph/archive, root frames, and declared input artifacts under its own confined staging root;
- status, claims, progress, logs, errors, cancellation, and public returns use the run store;
- canonical cache and views stay orchestrator-local;
- client reconnects without that filesystem;
- returned assets materialize under the caller-selected local root.

Test client exit immediately after intent, during ambiguous submission, after receipt, while queued, while running, during cancellation, and during finalization.

### WP9 — Cross-Feature and Crash Hardening

Required compositions:

- each scheduler backend with filesystem run store;
- scheduler simulator with remote run store;
- local and scheduler launch with staged ProcessingTool;
- staged ProcessingTool followed by remote DataFrameTool;
- remote DataFrameTool followed by staged ProcessingTool;
- custom-source bundle on both tool kinds;
- cancellation and failure in every stage;
- reconnectable exact result loading with file and directory assets.

Inject failures after every durable boundary.
Recovery must converge without rerunning workflow code after startup, publishing partial records, adopting the wrong scheduler job, or exposing worker-local paths.

Use dedicated read-only race and storage audits before leaving this package.

### WP10 — Acceptance and Release Hardening

Add ownership entries and focused commands before broad validation.
Keep every production module below the repository ceiling and every test module below its test ceiling.

Validate optional imports:

- ordinary `bioimageflow` import loads no Parsl, scheduler client, S3 client, pandas worker, or stager implementation unnecessarily;
- missing optional extras fail with one actionable error;
- package artifacts include every worker entry point and schema module;
- Python version boundaries remain enforced.

Run the complete repository validation matrix documented in Section 12.

### WP11 — Documentation and GUI Handoff

Update current documentation to the final architecture.
Remove planning status, superseded schemas, unsupported-backend statements, and local-only claims that are no longer true.

Write a concise GUI integration guide covering:

- scheduler configuration factory references;
- submitting/queued states and scheduler diagnostics;
- run-store locations and credentials;
- reconnection without canonical filesystem access;
- removal of client-facing control/view directory paths;
- result materialization roots and durable URI values;
- staged executor capabilities;
- remote `DataFrameTool` metadata;
- cache-browser limitations for orchestrator-local canonical storage;
- errors and cancellation presentation.

The guide does not preserve old GUI behavior, document migration shims, or constrain the library implementation.

## 11. Acceptance Matrix

### 11.1 Scheduler Launchers

- Every scheduler backend validates configuration before run allocation or external action when possible.
- Submission intent is durable before scheduler submission.
- One receipt produces one queued run and one job ID.
- Receipt-write crashes recover by token or verified job self-report without duplicate submission.
- Delayed scheduler visibility does not cause resubmission.
- Ambiguous submission never causes automatic resubmission without a verifiable idempotency or definitive-rejection guarantee.
- Duplicate token matches fail closed.
- Query outages do not become job absence.
- Native queued/running/completed/failed/cancelled/timeout states map deterministically.
- Queued cancellation commits intent before scheduler cancellation.
- Job cancellation after workflow startup produces `lost` unless graceful workflow cancellation completed.
- Scheduler logs are correlated, ordered, bounded, and secret-redacted.
- Client restart can observe and cancel from run store plus run ID.
- Scheduler adapters never allocate Parsl worker blocks directly.

### 11.2 Run Store and Client Separation

- Filesystem and S3-compatible stores pass one conformance suite.
- Mutable state uses real compare-and-swap.
- Claims exclude concurrent orchestrators and recover only after expiry and absence confirmation.
- Progress and scheduler observations have global monotonic sequence.
- Partial uploads and stale reads fail closed.
- Client and orchestrator use different local paths in integration tests.
- Workflow archives, root DataFrames, constants, and DataFrame path cells cross the client-orchestrator boundary without a client path appearing as a transport location.
- `WorkflowRun.result()` needs no canonical-cache path.
- `WorkflowRun` exposes no local control/view path when the selected store is remote.
- Owned result assets materialize only under the requested root.
- Credentials and signed URLs are absent from all durable artifacts and logs.

### 11.3 Staged Processing

- No orchestrator absolute path appears in encoded task/result payloads.
- Only declared path fields are rewritten.
- Arbitrary strings that look like paths remain unchanged.
- Scalars and indexes round-trip exactly.
- Files, empty directories, nested trees, and custom bundles verify digests.
- Shared and staged modes produce equal logical DataFrames, result keys, record IDs, and manifests.
- External URI identity is independent of signed transfer URL.
- Mutable unversioned URIs disable reusable caching.
- Worker-local scratch and extraction paths never persist.
- Failed, cancelled, or corrupt transfers publish no current record.
- Non-cancellable writers and transfers drain before cleanup eligibility.

### 11.4 Remote DataFrameTool

- Non-opted-in tools remain local.
- Opted-in source, transform, and merge tools execute once on the worker.
- Merge and transform are not split across hosts.
- Whole-frame input order and index semantics match local execution.
- Static, dynamic, passthrough, renamed, empty, zero-column, categorical, datetime, and multi-index frames validate.
- Input and output logical digests are checked separately from transport digests.
- Path columns and owned assets collect correctly.
- Undeclared worker paths fail closed.
- Environment, origin, resource, runtime, and stager mismatches fail preflight.
- Remote exceptions retain node, task, executor, origin, and traceback identity.
- Cancellation drains the future and publishes no partial record.
- Cache hits skip DataFrame transfer and worker startup when no other work requires them.
- Local and remote execution of the same declared tool/environment produces equal cache identity.
- No partition or streaming behavior appears.

### 11.5 Cross-Feature

- A scheduler-launched, remotely controlled run can use staged processing and remote DataFrame nodes in one workflow.
- Root DataFrames, archive custom sources, ordered ad hoc targets, zero-output workflows, and nested workflows remain reconnectable.
- Cancellation races with scheduler receipt, worker transfer, DataFrame completion, return installation, and finalization converge exactly.
- Crash recovery never reruns workflow code after startup.
- Return snapshots never depend on current cache selection.
- Direct, Wetlands, attached Parsl, and submitted Parsl retain shared backend-neutral semantics where applicable.

## 12. Validation and Commit Gates

Use the focused ownership selector during edits:

```bash
git diff --name-only | uv run python scripts/affected_tests.py --stdin
git diff --name-only | uv run python scripts/affected_tests.py --stdin --stage precommit
```

Every work package runs:

```bash
uv run ruff check .
uv run pyright
uv run pytest tests/unit/test_development_workflow.py
uv run python scripts/check_file_sizes.py
uv run python scripts/check_import_boundaries.py
```

Protocol and storage packages run pure and multiprocessing race tests before real Parsl tests.

Before integration:

```bash
uv run pytest tests/unit -m "not slow and not acceptance and not packaging and not package_tools and not complete and not wetlands and not public_data and not external_binary and not sairpico_binary and not model_runtime and not parsl"
uv run pytest tests/integration -m "not slow and not acceptance and not packaging and not package_tools and not complete and not wetlands and not public_data and not external_binary and not sairpico_binary and not model_runtime and not parsl"
uv run pytest tests -m "parsl and not slow"
uv run sphinx-build -W --keep-going docs/source docs/_build/html
```

Run scheduler-site, S3-service, process-isolation, slow Parsl, packaging, and acceptance selectors in their documented CI jobs.

Each work package is committed separately after its exit gate.
Do not mix unrelated cleanup, generated artifacts, progress notes, or pre-existing changes into those commits.

## 13. Principal Risks and Required Reviews

### External submission is not a filesystem transaction

The intent/token/receipt protocol is mandatory.
A scheduler adapter that cannot prove idempotency or definitive rejection after an ambiguous submission cannot resubmit automatically.

### Remote stores may not provide the required consistency

The run-store conformance suite must test the actual conditional primitives.
An adapter without strong per-key conditional writes is unsupported for mutable launcher state.

### Transfer success does not imply task success

Stage-in, execution, stage-out, collection, publication, and cleanup have distinct terminal evidence.
The launcher and cache must not collapse them into one optimistic future result.

### Directory and bundle extraction are security boundaries

Canonical manifests, size limits, traversal rejection, symlink rejection, and extraction confinement receive dedicated adversarial tests.

### Remote DataFrame execution can move very large frames

Whole-frame mode is explicit and observable.
The plan does not hide transfer cost or add automatic partitioning.
Diagnostics record frame sizes and transfer durations without adding them to cache identity.

### Public path behavior changes for remote clients

Remote owned assets require explicit local materialization and durable external references may be URI values rather than local paths.
The final GUI guide must call this out clearly.

### Cross-feature state space is large

The plan uses phase exits, shared conformance suites, and limited compositions before the final Cartesian integration set.
Agents must update the plan at work-package boundaries rather than attempting the entire program in one uninterrupted pass.

## 14. Execution Readiness

Implementation can start after this plan is approved.
WP0 requires no external service.

Before WP3 real-site acceptance, obtain command/version examples or test access for each scheduler that will be claimed as site-validated.
Before WP4 real-service acceptance, select the supported S3-compatible client/version range and CI test service.
Those environment inputs do not block protocol, simulator, conformance, or local integration work.

The recommended implementation order is WP0 through WP11 as written.
Remote `DataFrameTool` must not be pulled forward ahead of the artifact and worker protocol gates, and client-orchestrator no-shared claims must not be made until the run-store integration gate passes.
