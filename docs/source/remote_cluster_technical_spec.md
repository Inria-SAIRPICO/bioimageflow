# Remote cluster deployment and execution specification

## 1. Status and scope

This document is the normative implementation target for the public experience described in [Remote cluster experience and public API proposal](remote_cluster_experience_spec.md).

The API described here is not implemented yet.
Until implementation is complete, the current submitted-execution contract in [BioImageFlow Library Specifications](specs.md) remains authoritative for released code.

The words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are normative.

This specification covers:

- automatic installation or reuse of a user-owned BioImageFlow runtime on an SSH-accessible cluster;
- immutable environment, setup, Parsl, workflow, and input preparation;
- non-allocating validation and planning;
- PSI/J submission of the BioImageFlow orchestrator;
- Parsl allocation and routing of processing workers;
- durable run attachment, progress, diagnostics, cancellation, retry, and result download;
- JSON-safe integration surfaces for a BioImageFlow execution UI.

This specification does not define a new scheduler language.
Parsl worker configuration remains ordinary trusted Python.

The implementation MAY reuse current launcher, upload, planning, diagnostic, and result-bundle internals, but the superseded low-level remote API is not a compatibility constraint.

## 2. Resolved design decisions

The following decisions resolve the open questions in the experience proposal.

1. Managed worker initialization is validated through versioned BioImageFlow adapters for explicitly supported Parsl executor and provider combinations.
   Each adapter MUST read and validate the provider's public worker-initialization value and MUST reject an unsupported combination instead of inspecting arbitrary private attributes.
2. `WorkerSlot` remains explicit for CPU, GPU, memory, and GPU memory.
   BioImageFlow MAY compare it with reliable public executor values, but MUST NOT infer missing memory or GPU guarantees from scheduler syntax.
3. A bare `pyproject.toml` environment is not part of the first implementation because resolution after confirmation would weaken reproducibility and complicate offline behavior.
4. `from_existing_python()` accepts versioned or unversioned absolute paths, but always labels them externally managed and binds each plan to a fresh observed attestation.
   Submission MUST fail if that attestation changes after confirmation.
5. A published BioImageFlow installation uses a verified wheel for the exact running version.
   An editable or unreleased checkout is built into a non-editable wheel locally, and that wheel's digest defines the bootstrap artifact.
6. uv and Pixi local project members are captured as built distribution artifacts selected by their build metadata, never by recursively uploading an entire repository.
7. Mutable cluster-resident setup scripts are not supported initially.
   Every cluster-resident setup source requires an expected SHA-256 digest.
8. Remote results default to `<cluster.root>/results/<workflow-id>`.
   An advanced `results_root` MAY select another absolute shared path and receives the same ownership, symlink, and visibility validation.
9. Garbage collection is explicit and plan-based.
   Submission MUST NOT automatically delete deployments, uploads, run records, or results.

## 3. Public model

### 3.1 `RemoteCluster`

`RemoteCluster` is the public execution target for remote submitted execution.

Its constructor is:

```python
RemoteCluster(
    *,
    host: str,
    root: str | PurePosixPath,
    environment: ClusterEnvironment | None = None,
    parsl: ParslConfiguration | None = None,
    orchestrator: SchedulerJob | None = None,
    setup: SetupScript | None = None,
    results_root: str | PurePosixPath | None = None,
    connect_timeout: float = 15.0,
)
```

`host` is an OpenSSH alias or `user@host` value without embedded options.
`root` and `results_root` are normalized absolute POSIX paths.
`root` MUST identify a dedicated user-controlled subtree.
The first implementation restricts the root used in the stable SSH command to the safe token characters `[A-Za-z0-9._/+:@-]` and rejects whitespace or shell metacharacters.

An object containing only `host`, `root`, and connection options is an attach-only cluster handle.
`check_connection()`, `attach()`, capability inspection, and cleanup inspection MAY use an attach-only handle.
`deploy()`, `validate()`, `plan()`, `prepare()`, and `submit()` MUST report `configuration-incomplete` when their required configuration is absent.

`RemoteCluster` MUST NOT contain passwords, private keys, secret values, a live SSH connection, a live PSI/J object, or a live Parsl object.

Its configuration has the schema `bioimageflow.remote_cluster.v1` and round-trips through `to_dict()` and `from_dict()`.
The serialized form contains secret reference names but never resolved values.

### 3.2 `ClusterEnvironment`

`ClusterEnvironment` is a frozen tagged value with these first-version constructors:

```python
ClusterEnvironment.from_uv_project(path, *, groups=(), extras=(), package=None)
ClusterEnvironment.from_pixi_project(path, *, environment="default")
ClusterEnvironment.from_pylock(lock, *, project=None, groups=(), extras=())
ClusterEnvironment.from_wheelhouse(path, *, lock)
ClusterEnvironment.from_existing_python(path)
```

There is no first-version `from_pyproject()` constructor.
Unrecognized environment formats MUST fail without fallback guessing.

The value records only local source locations and non-secret selection options before preparation.
Its prepared representation replaces local paths with immutable artifact entries and digests.

The serialized schema is `bioimageflow.cluster_environment.v1`.

### 3.3 `SetupScript`

`SetupScript` replaces `PreLaunchScript` for managed cluster execution.

It provides:

```python
SetupScript.from_text(text)
SetupScript.from_file(path)
SetupScript.from_cluster_file(path, *, sha256)
```

Text and local files MUST be non-empty UTF-8 Bash source no larger than 64 KiB.
They MUST reject NUL bytes, symlinks, special files, and mutation during snapshotting.
Bytes MUST be preserved without newline normalization.

A cluster file MUST use a normalized absolute path and a required `sha256:<hex>` digest.
The bootstrap MUST verify that digest before sourcing the file.

The same verified script is sourced before:

- discovering the bootstrap Python;
- installing or invoking the cluster gateway;
- environment creation and validation;
- the PSI/J orchestrator command;
- every managed Parsl worker command through `runtime.worker_init`.

BioImageFlow appends deployment activation after the setup script.
The setup script MUST NOT be used as a package installation hook or mutate a published deployment.

Setup source bytes and local source paths MUST be absent from `repr()`.
Serialized public values contain source kind, size, and digest, plus a cluster path only for a pinned cluster source.

### 3.4 `SchedulerJob`

`SchedulerJob` describes only the orchestrator allocation.

Its first-version scheduler values are `"slurm"`, `"pbs"`, and `"lsf"`, subject to installed PSI/J executor support on the target.

It contains:

- positive `walltime`;
- optional queue or partition;
- optional project or account;
- positive CPU count;
- optional positive memory and GPU requirements;
- optional scheduler-adapter attributes containing finite JSON-safe scalar values;
- optional hard-cancellation grace.

Scheduler-adapter attributes MUST be validated by a named scheduler adapter.
They MUST NOT accept shell fragments, native directive strings, environment scripts, credentials, callbacks, or live PSI/J values.

`SchedulerJob` has schema `bioimageflow.scheduler_job.v1`.

### 3.5 `ParslConfiguration`

The common constructor is:

```python
ParslConfiguration.from_file(
    path,
    *,
    factory="build",
    kwargs=None,
    secret_refs=None,
    include=(),
)
```

`path` identifies one local Python source file.
`factory` is one identifier within that file, not an importable module string the user must install.
`include` explicitly names additional local Python files or package directories required by the factory.
No parent directory is uploaded implicitly.

Local configuration sources are snapshotted, validated as regular files or packages, installed under a deployment-owned generated module name, and included in deployment identity.

An advanced `from_module("module:function", ...)` constructor MAY refer to code already provided by an externally managed environment.
Its module distribution and observed environment attestation become plan-critical external claims.

`kwargs` contains finite JSON-safe values.
`secret_refs` maps factory argument names to cluster environment-variable names or versioned secret-provider references.
The same argument MUST NOT occur in both mappings.

`ParslConfiguration` has schema `bioimageflow.parsl_configuration.v1`.

### 3.6 Factory runtime and result

BioImageFlow calls the trusted factory as:

```python
result = build(runtime, **kwargs, **resolved_secrets)
```

`runtime` is a live `ParslFactoryRuntime` available only inside validation and orchestration processes.
It exposes read-only deployment paths, generated worker initialization, environment identity, storage mode, BioImageFlow core requirement, supported managed tool origins, and `executor_binding()`.

Its public representations contain no secret values.
It is never serialized as a persistence value.

The factory MUST return exactly `ParslFactoryResult`:

```python
ParslFactoryResult(
    *,
    config: parsl.Config,
    executor_bindings: Mapping[str, ExecutorBinding],
)
```

The live `parsl.Config` remains process-local.
Only sanitized validation facts and normalized executor bindings cross process boundaries.

The factory MAY import Parsl and construct executors and providers.
It MUST NOT load or initialize a DataFlowKernel, submit a job, create a workflow run, start workers, open a listening service, or mutate the published deployment.

It MUST be deterministic for the deployment, declared arguments, secret-presence state, and documented runtime values.
Secret values MAY affect authentication configuration but MUST NOT change executor labels, bindings, retry policy, or routing claims.

### 3.7 `WorkerSlot`

`WorkerSlot` is the concise resource declaration accepted by `runtime.executor_binding()`.

It supports positive CPU cores, non-negative GPU count, optional positive memory bytes, and optional positive GPU-memory bytes.
Human-readable byte strings MAY be accepted at construction but MUST normalize to integer bytes in serialized reports.

The slot describes the capacity guaranteed to one concurrent BioImageFlow task.
It does not describe a complete scheduler node and does not replace a tool's `ResourceSpec.max_concurrent`.

`runtime.executor_binding(slot=...)` fills the managed environment identity, storage claim, BioImageFlow core requirement, and deployment-owned tool origins.
Advanced external worker environments continue to use the complete `ExecutorBinding` contract.

### 3.8 Handles and reports

The public lifecycle uses:

- `ClusterDeployment` for one published or externally attested deployment;
- `PreparedClusterInvocation` for locally owned immutable workflow and input bytes;
- `ClusterValidationReport` for structured non-allocating remote validation;
- `RemoteExecutionPlan` for one confirmed routing and submission attempt;
- `RemoteWorkflowRun` for durable run observation and control;
- `ClusterCleanupPlan` and `ClusterCleanupReport` for explicit cleanup.

Every value that crosses a process boundary MUST have a versioned schema plus strict `to_dict()` and `from_dict()` methods.
Resource-owning local handles MAY keep private live state in addition to a public serialized summary.

### 3.9 Lifecycle signatures

The primary methods are:

```python
cluster.check_connection() -> ClusterConnectionReport
cluster.deploy(*, progress=None) -> ClusterDeployment
cluster.prepare(workflow, *, inputs=None, targets=None, node_input_overrides=None, task_policy=None, lifetime=3600) -> PreparedClusterInvocation
cluster.validate(*, deployment, timeout=None) -> ClusterValidationReport
cluster.plan(prepared, *, deployment, validation=None, lifetime=1800) -> RemoteExecutionPlan
cluster.submit(workflow, *, inputs=None, targets=None, node_input_overrides=None, task_policy=None, progress=None) -> RemoteWorkflowRun
cluster.attach(run_id) -> RemoteWorkflowRun
cluster.plan_cleanup(...) -> ClusterCleanupPlan
cluster.apply_cleanup(plan) -> ClusterCleanupReport
```

`PreparedClusterInvocation` and `RemoteExecutionPlan` are context-manageable and explicitly closable.
`RemoteExecutionPlan.submit()` returns `RemoteWorkflowRun` and is the only plan mutation.

Public methods use keyword-only consequential options.
They MUST reject unknown options rather than silently forwarding them to SSH, PSI/J, Parsl, uv, Pixi, or a scheduler.

## 4. Cluster root and storage

### 4.1 Required layout

BioImageFlow owns these namespaces beneath `cluster.root`:

```text
<root>/
├── gateway/
├── deployments/
├── objects/
├── operations/
├── runs/
├── transfers/
├── results/
└── temporary/
```

`gateway/` contains versioned bootstrap and protocol gateway artifacts.
`deployments/` contains published runtime deployments addressed by digest.
`objects/` contains immutable uploaded objects shared by retained references.
`operations/` contains idempotency receipts.
`runs/` contains durable run control and observation data.
`transfers/` contains bounded result-download snapshots.
`results/` contains workflow runtime storage by workflow ID.
`temporary/` contains unpublished candidates and interrupted-operation material eligible for cleanup.

The implementation MAY add versioned intermediate directories but MUST preserve these ownership and lifetime boundaries.

### 4.2 Path and permission rules

Every managed path MUST be normalized, absolute after joining to `root`, confined to its namespace, and traversed without following symlinks.

The gateway MUST reject a symlink in any managed path component, an existing special file, unexpected ownership, group- or world-writable control or deployment directories, and hard-link or rename operations that escape a managed namespace.

New private directories use mode `0700`, regular control files use `0600`, and executable wrappers use `0700`, subject only to a more restrictive filesystem policy.
Remote processes run as the SSH user and MUST NOT request privilege, change ownership, or weaken permissions.

The selected root and results root MUST be visible at identical absolute paths from the login node, orchestrator, and managed worker nodes.
Non-allocating validation records compute-node visibility as unverified until worker startup.

### 4.3 Workflow storage

Remote execution owns its runtime storage selection.
A laptop-local workflow storage path MUST NOT be interpreted as a remote path.

The default remote workflow storage is `<results_root or root/results>/<workflow-id>/`.
The workflow ID is the stable public workflow identity already used by graph serialization.

Storage paths, cluster roots, deployment IDs, executor routes, and portable resource overrides MUST NOT enter processing result or cache keys unless the existing cache contract already includes the corresponding semantic tool or environment version.

The target API permits a workflow definition without an attached storage path.
Local execution MUST receive storage from its local execution context or an explicitly bound workflow runtime.
Remote execution always uses the selected remote results root.

## 5. Bootstrap and gateway

### 5.1 Prerequisites

The initial bootstrap requires OpenSSH, SFTP, non-interactive Bash, a SHA-256 utility, and a compatible Python made visible by the selected setup script.
These are site prerequisites, not packages BioImageFlow attempts to install with privilege.

### 5.2 Bootstrap boundary

The laptop package contains or constructs a versioned bootstrap artifact with a published digest and protocol range.

Bootstrap uses system OpenSSH and SFTP with argument arrays and `shell=False` locally.
It honors the user's ordinary SSH configuration, agent, jump hosts, and host-key policy.
It MUST NOT accept passwords, private-key bytes, arbitrary SSH option strings, or host-key bypass values.

Before a gateway exists, the only permitted remote command is a library-owned constant Bash bootstrap program.
User values are transferred through a length-delimited stdin envelope or safe encoded data fields, never interpolated as executable shell text.

The bootstrap program may:

1. validate or create the dedicated root with private permissions;
2. allocate a private server-named partial upload location;
3. receive bootstrap artifacts through SFTP;
4. verify sizes, SHA-256 digests, file types, and confined paths;
5. verify and source the setup script;
6. invoke the discovered Python to install a versioned gateway candidate;
7. atomically publish that gateway;
8. return a canonical JSON capability response.

It MUST NOT submit a scheduler job, install outside `root`, invoke `sudo`, modify shell startup files, or install workflow dependencies.

### 5.3 Stable gateway entry

After bootstrap, `<root>/gateway/entry` is the stable remote command used by `RemoteCluster`.
It is a small root-owned dispatcher, not a daemon.
SSH invokes it once per bounded request and it exits after one response.

Each immutable gateway publication binds its bootstrap setup digest, Python identity, executable environment, gateway artifact digest, and supported protocols.
The stable entry sources the gateway-owned verified setup copy when necessary and invokes the gateway-owned interpreter by absolute path.
It does not depend on the caller retaining a `SetupScript` object or rerun environment discovery during attachment.

The dispatcher selects an immutable compatible gateway implementation by protocol version or retained run metadata.
A guarded gateway upgrade MAY atomically update dispatcher metadata, but MUST NOT modify an immutable gateway or change the deployment bound to an existing run.

This stable entry is what permits `RemoteCluster(host=..., root=...).attach(run_id)` without the original project files or setup object.

### 5.4 Gateway protocol

Requests use `bioimageflow.cluster.request.v1` and responses use `bioimageflow.cluster.response.v1`.

Each request contains protocol version, UUID4 request ID, operation name, canonical JSON-safe arguments, and a payload digest when uploaded bytes are referenced.
Each response contains the matching request ID, success or failure status, structured payload or sanitized diagnostic, and gateway and supported protocol versions.

Mutating operations also carry a stable operation ID and canonical request digest.
The gateway persists an operation receipt before acknowledging a completed mutation.
Repeating an equal operation returns the same logical result, while reusing an operation ID with different bytes fails with `operation-conflict`.

Uploads use only server-issued partial paths.
Publication verifies the complete manifest and atomically renames a confined candidate.
Client-supplied absolute upload destinations are forbidden.

## 6. Environment preparation

### 6.1 Common rules

Every managed environment MUST include compatible exact versions of BioImageFlow, Parsl, PSI/J, the selected PSI/J scheduler plugin, the workflow's orchestrator-side packages, and packages promised by managed executor bindings.

The submitted client BioImageFlow artifact is authoritative.
If an environment lock resolves an incompatible BioImageFlow version, deployment fails with `bioimageflow-version-conflict` rather than silently selecting one side.

Local editable packages are built into wheels before deployment.
Editable installs and mutable source-tree imports are forbidden in a published deployment.

Build output, installer version, selected groups or extras, target platform, lock data, and artifact digests are identity-bearing.
Package installation is non-interactive and MUST NOT update a supplied lock.

### 6.2 uv projects

`from_uv_project()` requires a `pyproject.toml` and an up-to-date `uv.lock` in the selected project root.

The selected groups, extras, package, workspace members, indexes by non-secret identity, and target platform are explicit prepared inputs.
The lock MUST be installed with frozen semantics.

Local workspace members selected by the resolution are built through their declared build backends into immutable distributions.
Files included by those build artifacts are captured; unrelated repository files are not uploaded.
An unpackageable local dependency fails before remote installation.

### 6.3 Pixi projects

`from_pixi_project()` requires `pixi.toml` or a Pixi-enabled `pyproject.toml`, an up-to-date `pixi.lock`, and a named environment present in that lock.

The selected environment MUST contain a target matching the observed cluster platform.
Installation uses locked or frozen Pixi semantics and MUST NOT re-solve on the cluster.

A compatible Pixi bootstrap artifact is verified and installed under the cluster root when required.
Conda and PyPI artifacts selected by Pixi are recorded in the deployment manifest.

### 6.4 Standard Python locks

`from_pylock()` accepts a PEP 751 `pylock.toml` supported by the selected installer.
The selected dependency groups, extras, environment markers, Python requirement, artifact URLs or local artifacts, sizes, and hashes are validated before publication.

Mutable VCS references, unhashed archives, or unsupported local-directory entries fail unless preparation converts them into an immutable built artifact.

### 6.5 Offline wheelhouses

`from_wheelhouse()` requires a standard lock plus every artifact selected for the observed target platform.

Preparation MUST prove that the wheelhouse contains exactly one compatible locked candidate for every required distribution and that every expected hash matches.
Source distributions and network retrieval are forbidden for this environment kind.

Installation sets installer offline mode and records that no package index was contacted.

### 6.6 Existing Python

`from_existing_python()` never modifies the selected interpreter.

Validation executes a bounded attestation helper with that interpreter and records:

- normalized interpreter path;
- Python implementation, version, ABI, platform, and executable file identity;
- exact installed distribution names and versions required by the run;
- hashes of relevant distribution metadata and `RECORD` files when available;
- BioImageFlow, Parsl, PSI/J, scheduler plugin, and tool-origin compatibility;
- setup-script digest and declared shared-path visibility.

The attestation is an external claim, not a content-owned deployment.
It is included in the plan and repeated immediately before submission and worker acceptance.
A changed attestation fails with `external-environment-changed`.

## 7. Deployment identity and publication

### 7.1 Identity manifest

The deployment manifest uses `bioimageflow.cluster_deployment_manifest.v1`.

Its identity-bearing section contains:

- schema and deployment protocol versions;
- observed cluster operating system, architecture, and Python ABI target;
- setup source kind and digest;
- environment kind, selections, manifest and lock digests, resolved package set, and artifact digests;
- exact BioImageFlow bootstrap distribution and gateway compatibility digests;
- Parsl source bundle digest, factory name, JSON-safe arguments, and secret reference names;
- generated factory-runtime contract version;
- supported managed provider-adapter versions;
- installer and build-tool versions that affect output;
- selected local project distribution digests.

Secret values, timestamps, temporary paths, hostnames that do not affect compatibility, and scheduler queue availability are excluded.
The deployment ID is `sha256:<hex>` over canonical JSON bytes of the identity-bearing section.

Externally managed deployments use the same ID form but include the observed attestation digest and `ownership="external"`.
Reusing the ID never upgrades an external claim into content ownership.

### 7.2 Deployment state

Deployment state is:

```text
absent → preparing → installing → validating → published
                                  ↘ failed
```

Only `published` is visible to planning and submission.
Candidates use server-generated private names and become visible through one atomic publication.

Concurrent attempts for the same deployment ID coordinate through an ownership record.
They either reuse a verified published deployment, wait for the active installer, or replace a provably abandoned candidate.
They MUST NOT merge partial files.

Every published content-owned deployment is made read-only and verified against its manifest before reuse.
A mismatch reports `deployment-tampered`; BioImageFlow MUST NOT repair it in place.

`cluster.deploy()` creates no workflow run, initializes no DataFlowKernel, allocates no worker, and submits no scheduler job.

## 8. Parsl resolution, bindings, and worker startup

### 8.1 Isolated factory validation

Factory import and invocation occur in a short-lived child process inside the selected deployment.
The process uses a bounded timeout, a private temporary directory, a minimal inherited environment, and resolved secret references.
It is isolation for cleanup and diagnostics, not a security sandbox for untrusted code.

The child returns only a sanitized normalized validation payload.
Live Parsl objects are discarded when the child exits.
Cleanup MUST terminate child processes and remove temporary state on success, failure, and timeout.

Validation requires:

- exactly one `ParslFactoryResult`;
- `Config.retries == 0`;
- unique non-empty Parsl executor labels;
- exact equality between executor labels and binding labels;
- finite normalized bindings;
- compatible portable slot capacities;
- allowed tool origins and environment identities;
- shared-filesystem storage mode for the first managed implementation;
- a supported worker-initialization adapter for every managed executor;
- exact installation of `runtime.worker_init` according to that adapter;
- no DataFlowKernel or scheduler activity.

Missing secret references are reported by reference name only.
Resolved values are redacted from child stdout, stderr, exceptions, tracebacks, reports, and representations controlled by BioImageFlow.

### 8.2 Managed provider adapters

Each supported executor or provider adapter is versioned and declares compatible Parsl package versions and concrete public types, how to read the public worker-initialization field, how to compare it with `runtime.worker_init`, which provider settings can be normalized safely, and whether the provider supports the required shared-root and nested-submission topology.

Unknown or incompatible types fail with `unsupported-managed-provider`.
The implementation MUST NOT fall back to private attribute probing or assume worker initialization was applied.

An advanced external binding MAY bypass managed initialization only when it explicitly declares an externally managed worker environment and passes the existing origin, core-version, storage, and resource checks.

### 8.3 Launch-time comparison

The orchestrator invokes the same factory again immediately before initializing Parsl.
BioImageFlow normalizes its safety-relevant result and compares it with the confirmed plan.

The comparison includes executor labels, bindings, worker initialization, retry policy, environment identities, storage mode, managed tool origins, slot capacities, and supported normalized provider settings.
A difference fails the run before DataFlowKernel creation with `parsl-configuration-changed`.

### 8.4 Worker startup acknowledgement

Before a managed executor route accepts workflow tasks, at least one worker on that route MUST acknowledge the exact deployment ID, setup and activation marker, BioImageFlow core compatibility, shared storage visibility, tool-origin availability, observable declared devices, and connectivity to the orchestrator.

The acknowledgement is a runtime check and may require an allocated worker.
It is not part of non-allocating validation.

A failed route remains unavailable and produces a structured route diagnostic.
Tasks MUST NOT be sent to it speculatively.

## 9. Workflow and invocation preparation

### 9.1 Prepared invocation

`cluster.prepare()` returns `PreparedClusterInvocation` and performs no network operation.

It snapshots canonical recursive workflow serialization, selected targets or public workflow inputs, every explicit `LocalUpload`, invocation-only node path overrides, locally supplied workflow or tool source artifacts required by graph reconstruction, task policy, and portable node resource overrides.
It records cluster-resident paths as typed external references without probing them on the laptop.

Original laptop paths and secret values are absent from the public manifest.
Each entry records a relative logical name, kind, size, and SHA-256 digest.

The schema is `bioimageflow.prepared_cluster_invocation.v1`.
Its invocation digest is computed over canonical graph, invocation, and entry manifests.

### 9.2 Ownership and lifetime

Preparation copies all local meaning-bearing bytes into a private local directory.
Later changes or deletion of the originals cannot affect the prepared invocation.

The handle is context-manageable, explicitly closable, and has a finite configurable lifetime.
Closing or expiry removes only unsubmitted private local copies.

Planning retains the prepared handle.
Successful or uncertain submission consumes it and transfers ownership to the run attempt.
A definitively rejected pre-submission attempt MAY leave it reusable when the structured result explicitly says no remote mutation occurred.

### 9.3 Input semantics

Only `LocalUpload(Path(...))` reads laptop bytes.
An ordinary `Path` in a remote invocation MUST be a normalized absolute cluster path.
A path-looking string remains a string.

Recursive path collections and invocation-only node overrides retain the current public shape and validation rules.
Unknown, connected, workflow-boundary, non-path, relative unmarked, or type-incompatible overrides fail before upload.

### 9.4 Workflow definition and storage

Reusable workflow serialization excludes runtime storage.
`RemoteCluster` binds remote storage during planning.

A workflow object MAY carry a local runtime storage binding for prior local execution, but remote preparation MUST neither serialize nor reinterpret that binding.
The public workflow construction API MUST permit the storage-free golden example.

## 10. Validation and planning

### 10.1 Connection check

`cluster.check_connection()` tests OpenSSH reachability and, when present, gateway protocol compatibility.
It writes no remote file, invokes no user setup or factory code, and submits no job.

On a fresh root it reports that bootstrap is required rather than bootstrapping implicitly.

### 10.2 Remote validation

`cluster.validate(deployment=...)` requires a published deployment.

It validates gateway and deployment integrity, setup and Python invocation, environment or external attestation, trusted Parsl factory behavior, referenced secrets, `retries=0`, executor labels and bindings, managed provider adapters, PSI/J support, paths, launch values, and shared-storage declarations.

It creates no workflow run, initializes no DataFlowKernel, allocates no worker, and submits no scheduler job.

`ClusterValidationReport` has schema `bioimageflow.cluster_validation_report.v1` and contains deployment ID, attestation digest, normalized bindings, verified/declared/unverified facts, sanitized diagnostics, validity and expiry metadata, and relevant gateway, adapter, and protocol versions.

The report MUST state that compute-node mounts, worker networking, nested submission policy, queue availability, future quotas, and hardware cannot be proven without allocation.

### 10.3 Planning

`cluster.plan(prepared, deployment=..., validation=None)` returns `RemoteExecutionPlan`.

When `validation` is omitted or stale, planning MAY perform the same non-mutating remote validation.
It MUST NOT deploy, install, create a run, submit a job, initialize a DataFlowKernel, or allocate a worker implicitly.

Planning uses the same recursive scope compilation, effective node requirements, environment compatibility, storage compatibility, tool-origin compatibility, executor capacity comparison, task policy, cache selection, and route selection as runtime dispatch.

For every scoped `ProcessingTool` node it returns scoped node path, effective resources, compatible executor labels, selected route and reason, environment/origin/storage compatibility, cache status, and structured incompatibility reasons.
`DataFrameTool` nodes remain visible as orchestrator work and expose no worker resource override.

The plan binds prepared invocation digest, deployment and external attestation, validation evidence and expiry, normalized factory claims, routing and cache revision, scheduler job, remote storage, stable submission attempt ID, and preallocated run ID.
Its schema is `bioimageflow.remote_execution_plan.v1`.

### 10.4 Plan lifetime

A plan has a finite expiry.
Expiry never changes its recorded meaning, but prevents submission until the user creates and confirms a new plan.

The plan owns or references the prepared local bytes needed for submission.
Its public confirmation summary is JSON-safe, but deserializing that summary alone does not recreate missing local owned bytes.

## 11. Submission and idempotency

### 11.1 Explicit submission

`plan.submit()` is the confirmation boundary.

It MUST verify local prepared bytes and plan digest, verify the exact deployment, revalidate external and plan-critical claims, upload only prepared objects, allocate the preselected run and attempt IDs durably, persist submission and scheduler intent, submit one PSI/J orchestrator job, persist its correlated receipt when available, and return `RemoteWorkflowRun`.

It MUST NOT reread original workflow, configuration, setup, project, or input paths.
It MUST NOT silently regenerate routes or select a different deployment.

### 11.2 Convenient submission

`cluster.submit(workflow, ...)` is exactly the composition of deployment preparation, invocation preparation, validation, planning, and `plan.submit()`.

It creates one plan and one stable attempt.
An internal retry caused by a lost acknowledgement reuses that attempt and MUST NOT prepare or plan again.

The method MAY expose structured progress callbacks for deployment and upload phases.
Those callbacks do not change semantics.

### 11.3 Operation receipts

The submission attempt ID is generated before the first mutating remote request.
All submission mutations are journaled under that ID.

Repeating `plan.submit()` returns the original run when accepted, resumes a safe incomplete transfer or pre-submit phase, reconnects to a known scheduler receipt, or reports `submission-uncertain` without resubmitting when scheduler acceptance cannot be disproved.
It never creates a second run ID or scheduler job.

`RemoteSubmissionUncertainError` contains host, root, run ID, attempt ID, stable category, sanitized message, and `next_action`.
It contains no secret value or live remote object.

### 11.4 Orchestrator launch

PSI/J launches only the BioImageFlow orchestrator.
Parsl providers inside that orchestrator request worker allocations.

The PSI/J job uses the exact deployment Python, verified setup copy, remote workflow storage, and run-owned submission.
It never receives an original mutable setup path or mutable deployment alias.

The orchestrator revalidates factory claims before Parsl initialization and records its exact launch environment in run provenance.

## 12. Run state, observation, and control

### 12.1 Run states

The durable run states remain `prepared`, `starting`, `running`, `finalizing`, `cancel_requested`, `succeeded`, `failed`, `cancelled`, and `lost`.

State changes use guarded revisions and claim epochs.
Only terminal success, failure, cancellation, or loss may start a retry plan.

The run binds the exact cluster root, storage path, deployment ID, invocation digest, execution-plan digest, attempt ID, scheduler intent, and scheduler receipt.

### 12.2 Attachment

`cluster.attach(run_id)` requires only host, root, connection policy, and run ID.
It MUST NOT require the original workflow, environment definition, setup source, Parsl source, or local prepared bytes.

Attachment is read-only until the caller explicitly invokes a mutating operation such as cancellation, retry, result preparation, or deletion.
It creates no new run or scheduler job.

Protocol negotiation MUST either select a compatible retained gateway or return a structured `protocol-incompatible` diagnostic.

### 12.3 Progress and diagnostics

Progress uses global monotonic sequences and scoped node paths.
Existing progress consumers remain compatible where their documented fields are unchanged.

Every failed scoped node has an independently persisted `NodeFailureDiagnostic` containing scoped node path, stable category, original exception type where available, sanitized message and traceback, terminal or retry state, and run/attempt/task/invocation/row identity where applicable.

Attached callbacks and reconnected inspection return the same logical diagnostic.
Consumers MUST NOT parse logs to obtain failure structure.

### 12.4 Cancellation

Cancellation is idempotent.
It durably records `cancel_requested` before signaling the orchestrator or scheduler.

Cooperative cancellation is attempted first.
Scheduler cancellation uses the retained PSI/J receipt after the configured grace period.
Confirmed forced termination records `lost` when worker and writer cleanup cannot be proven.

Calling cancel on a terminal run returns its terminal state without creating an error or new action.

### 12.5 Retry

The current public `RunRetryPlan` and recomputation semantics remain authoritative.
A remote retry reuses the retained deployment, invocation, input objects, and setup material after digest verification.
It never reads original laptop paths.

If the retained external Python attestation no longer matches, retry planning reports incompatibility and requires an explicit new deployment or a later acknowledgement extension.

## 13. Results and downloads

A successful run installs its exact public return before entering `succeeded`.
Historical result loading never consults mutable current-cache pointers.

`run.download_result(destination)` prepares and downloads the existing verified portable result bundle.

It MUST bind the exact run and return digest, include DataFrames and owned explanatory assets, retain declared external cluster paths as typed references, transfer through a private sibling, verify every entry, publish atomically, reject unrelated destinations, accept identical destinations idempotently, and safely retry interrupted transfer.

Preparing a result transfer creates no workflow run and submits no scheduler job.
Temporary transfer objects are protected while a client lease or retained download receipt references them.

## 14. Secrets and trusted code

Secret values are late-bound external inputs and never contribute to deployment, invocation, or plan digests.
Secret reference names and their argument mapping are identity-bearing.

BioImageFlow MUST redact values it resolves from manifests, schemas, public representations, structured diagnostics and events, controlled logs, child-process exception payloads, and operation receipts.
Missing secrets are reported only by reference name.

Setup scripts, Parsl factories, workflow code, package build hooks, scheduler wrappers, and cluster administrators are trusted executable parties.
BioImageFlow does not claim to sandbox them or prevent them from reading secrets made available to their process.

Local GUIs MUST show executable-source origin and digest before confirmation.
Cluster-resident executable code without a supplied digest is rejected.

## 15. Cleanup and quotas

### 15.1 No automatic eviction

Submission and deployment MUST NOT automatically remove prior deployments, input objects, run records, diagnostics, or results.
Interrupted unpublished candidates MAY be removed after a documented abandonment timeout when no active operation owns them.

### 15.2 Cleanup API

Cleanup is two-phase:

```python
cleanup = cluster.plan_cleanup(...)
report = cluster.apply_cleanup(cleanup)
```

`ClusterCleanupPlan` is a JSON-safe snapshot listing exact candidate paths, identities, sizes, reference reasons, and destructive consequences.
It creates no mutation.

`apply_cleanup()` revalidates every candidate and reference revision before deletion.
Any changed candidate is skipped with a structured conflict rather than broadening deletion.

Cleanup MUST refuse to remove a deployment referenced by an active run, an object referenced by a retained run or retry plan, a transfer with an active lease, or a run record not explicitly selected for terminal-run deletion.

Terminal-run deletion records the loss of attachment, diagnostics, retry, and possibly result availability.
Cleanup accepts no recursive arbitrary path supplied by the user.
All deletion targets come from gateway inventory and remain confined to managed namespaces.

### 15.3 Quota diagnostics

Deployment, upload, and submission estimate required bytes and inode counts when possible.
Failures use stable `quota-bytes`, `quota-inodes`, or `insufficient-space` categories and preserve safely reusable completed objects.

## 16. Operation effects

| Operation | Contacts cluster | Writes remote state | Runs trusted user code | Submits scheduler job | Creates run | Allocates workers |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `check_connection()` | Yes | No | No | No | No | No |
| `deploy()` | Yes | When absent | Setup and package builds | No | No | No |
| `prepare()` | No | No | Workflow serialization only | No | No | No |
| `validate()` | Yes | Temporary bounded state only | Setup and Parsl factory | No | No | No |
| `plan()` | Maybe | No | Factory only if validation refreshes | No | No | No |
| `plan.submit()` | Yes | Yes | Setup, factory, orchestrator | Yes | Yes | Later through Parsl |
| `cluster.submit()` | Yes | Yes | All composed phases | Yes | Yes | Later through Parsl |
| `attach()` and inspection | Yes | No | No | No | No | No |
| `cancel()` | Yes | Yes | No new code | May cancel retained jobs | No new run | No new workers |
| `download_result()` | Yes | Bounded transfer state | No | No | No | No |
| `plan_cleanup()` | Yes | No | No | No | No | No |
| `apply_cleanup()` | Yes | Deletes selected state | No | No | No | No |

Temporary files created by validation are not durable remote state and MUST be removed before the operation returns.
Because setup, factory, and package build hooks are trusted code, their own undeclared side effects cannot be prevented by this contract.

## 17. Diagnostics

Every public failure contains operation phase, stable category, sanitized human message, allocation state, retry safety, machine-readable `next_action`, and optional related deployment/invocation/plan/attempt/run/executor/node identities.

Allocation state is `none`, `orchestrator-submitted`, `workers-possible`, or `unknown`.
Retry safety is `safe`, `same-attempt-only`, `unsafe`, or `not-applicable`.

The first implementation defines at least these categories:

- `ssh-unavailable`;
- `ssh-timeout`;
- `bootstrap-prerequisite-missing`;
- `cluster-root-unsafe`;
- `setup-failed`;
- `setup-digest-mismatch`;
- `environment-lock-invalid`;
- `environment-artifact-missing`;
- `environment-platform-incompatible`;
- `bioimageflow-version-conflict`;
- `deployment-install-failed`;
- `deployment-tampered`;
- `external-environment-changed`;
- `secret-reference-missing`;
- `parsl-factory-failed`;
- `parsl-retries-enabled`;
- `executor-label-mismatch`;
- `unsupported-managed-provider`;
- `worker-initialization-missing`;
- `route-incompatible`;
- `validation-expired`;
- `plan-expired`;
- `parsl-configuration-changed`;
- `scheduler-rejected`;
- `submission-uncertain`;
- `worker-startup-failed`;
- `protocol-incompatible`;
- `operation-conflict`;
- `quota-bytes`;
- `quota-inodes`;
- `result-integrity-failed`;
- `cleanup-conflict`.

Logs remain available for expert investigation but are not the source of public status, routing, or failure semantics.

## 18. Serialization contract

### 18.1 Common encoding

Canonical payloads use UTF-8 JSON with sorted object keys, no insignificant whitespace, and no non-finite numbers.
Digests use lowercase `sha256:<64 lowercase hexadecimal characters>`.
Timestamps use UTC RFC 3339 with a `Z` suffix.
Durations, byte counts, sequence numbers, and resource counts use JSON numbers with the integer or finite-number constraints defined by their public type.

Every versioned schema has an exact required key set.
Readers reject missing keys, unknown keys, wrong scalar types, unsafe paths, unsupported versions, and duplicate logical identities.
Writers preserve deterministic list ordering where order is semantic and sort set-like collections by their canonical encoded value.

Serialized reports contain normalized resource bytes and paths, secret reference names, and sanitized diagnostics.
They never contain live Parsl or PSI/J objects, callbacks, credentials, original laptop paths, setup contents, private temporary paths, or owned artifact bytes.

### 18.2 Plan summary example

The public plan summary follows this shape:

```json
{
  "schema": "bioimageflow.remote_execution_plan.v1",
  "attempt_id": "7b8f61d4-2777-4d58-a952-f6601bb1de39",
  "run_id": "0d87dd5b-f27a-4794-8ee0-c59eb972f44b",
  "deployment_id": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "external_attestation_digest": null,
  "invocation_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
  "validation_digest": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
  "storage_path": "/cluster/project/alice/bioimageflow/results/measure-images",
  "expires_at": "2026-08-06T14:30:00Z",
  "scheduler_job": {
    "schema": "bioimageflow.scheduler_job.v1",
    "scheduler": "slurm",
    "queue": "compute",
    "project": "BIOIMAGE",
    "walltime_seconds": 14400,
    "cpu": 4,
    "memory_bytes": null,
    "gpu": 0,
    "attributes": {},
    "hard_cancel_after_seconds": null
  },
  "nodes": [
    {
      "node": "measure",
      "requirement": {
        "cpu": 1,
        "gpu": 0,
        "memory_bytes": 4294967296,
        "gpu_memory_bytes": null,
        "max_concurrent": null
      },
      "compatible_executors": ["cpu-workers"],
      "selected_executor": "cpu-workers",
      "route_reason": "unique-compatible-executor",
      "cache_status": "missing",
      "incompatibilities": []
    }
  ],
  "plan_digest": "sha256:3333333333333333333333333333333333333333333333333333333333333333"
}
```

The live plan additionally owns or references prepared bytes and the cluster client needed by `submit()`.
Those private resources are not reconstructed by `from_dict()`.
A deserialized summary is suitable for persistence, display, comparison, and later recovery lookup, but cannot submit without rebinding the retained local preparation through an explicit public recovery operation.

### 18.3 Gateway request and receipt example

A mutating submission request follows this envelope:

```json
{
  "schema": "bioimageflow.cluster.request.v1",
  "protocol_version": 1,
  "request_id": "bb0e65b1-f4ec-4f13-a040-70c214a65dc7",
  "operation": "submit_plan",
  "operation_id": "7b8f61d4-2777-4d58-a952-f6601bb1de39",
  "arguments": {
    "run_id": "0d87dd5b-f27a-4794-8ee0-c59eb972f44b",
    "plan_digest": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
    "uploaded_object_ids": [
      "sha256:4444444444444444444444444444444444444444444444444444444444444444"
    ]
  },
  "payload_digest": "sha256:5555555555555555555555555555555555555555555555555555555555555555"
}
```

A successful response follows:

```json
{
  "schema": "bioimageflow.cluster.response.v1",
  "protocol_version": 1,
  "request_id": "bb0e65b1-f4ec-4f13-a040-70c214a65dc7",
  "status": "ok",
  "payload": {
    "operation_id": "7b8f61d4-2777-4d58-a952-f6601bb1de39",
    "operation_digest": "sha256:6666666666666666666666666666666666666666666666666666666666666666",
    "run_id": "0d87dd5b-f27a-4794-8ee0-c59eb972f44b",
    "run_state": "starting",
    "scheduler_receipt_state": "installed"
  },
  "diagnostic": null,
  "gateway_version": "1.0",
  "supported_protocol_versions": [1]
}
```

An error response uses `status="error"`, `payload=null`, and one structured diagnostic with the fields required by Section 17.
The response never embeds raw stderr or a Python exception object.

### 18.4 Schema ownership

The cluster gateway validates request and upload schemas independently of the laptop.
The orchestrator validates the retained submission independently of the gateway before creating Parsl state.
The laptop validates observation and result schemas independently before exposing them publicly.

Schema version support is reported through capability discovery.
An unsupported version fails before mutation with `protocol-incompatible`.

## 19. Capabilities and optional dependencies

`get_execution_capabilities()` gains entries for remote cluster bootstrap, managed uv, managed Pixi, standardized Python locks, offline wheelhouses, existing-Python attestation, setup scripts, remote validation and planning, idempotent planned submission, durable diagnostics, and cleanup planning.

Capability inspection MUST NOT import Parsl, PSI/J, uv, Pixi, scheduler plugins, or SSH libraries eagerly.

The laptop installs remote orchestration support through `bioimageflow[cluster]`.
This extra provides bootstrap and environment-preparation support but does not require a local Parsl DataFlowKernel or local PSI/J scheduler plugin during ordinary workflow construction or capability inspection.

Parsl, PSI/J, the selected scheduler plugin, and installer tools are installed or verified inside the selected deployment.
The laptop still needs workflow and tool packages required to construct the graph.

## 20. Relationship to current APIs

This redesign intentionally does not preserve the current transported-submission API.

When implemented:

- `RemoteCluster` replaces user construction of `SSHSubmissionTransport`;
- `SchedulerJob` replaces the remote use of `PSIJLaunchConfig`;
- `SetupScript` replaces `PreLaunchScript`;
- `ParslConfiguration` replaces remote `ParslConfigRef` construction;
- `PreparedClusterInvocation` and `RemoteExecutionPlan` replace the flat `prepare_remote_submission()` call;
- `cluster.submit()` replaces `submit_workflow(..., transport=...)`;
- executor bindings move beside the Parsl `Config` in `ParslFactoryResult`;
- internal staging, remote executable, and shared-runtime paths disappear from the common public API.

No deprecated aliases or dual wire paths are required.
Superseded public exports, compatibility tests, migration documents, and obsolete examples MUST be removed in the same implementation.

Direct execution, Wetlands execution, attached Parsl routing, portable node resource overrides, cache keys, scoped node paths, progress, retry meaning, diagnostics, and result-bundle semantics remain behaviorally consistent unless this specification explicitly changes storage binding.

The current cluster protocol MAY be migrated internally when its immutable upload, run receipt, observation, cancellation, retry, and result semantics satisfy this contract.

## 21. Conformance tests

### 21.1 Public values and serialization

Tests MUST cover strict construction and round trips for public JSON-safe values, secret rejection and redaction, normalized paths/scheduler values/resources, and optional dependency inspection without Parsl or PSI/J imports.

### 21.2 Preparation and identity

Tests MUST prove immutable local snapshots, stable and sensitive deployment identity, exclusion of timestamps and secret values, non-editable wheel creation, bounded uv/Pixi source capture, tamper rejection, and concurrent equal deployment publication.

### 21.3 Environment adapters

Tests MUST cover successful and failing fixtures for locked uv, locked Pixi, `pylock.toml`, offline wheelhouse, and existing Python.
They MUST include stale locks, missing artifacts, hash mismatches, unsupported platforms, incompatible Python, conflicting BioImageFlow versions, forbidden source distributions in offline mode, and changed external attestations.

### 21.4 Bootstrap and security

Tests MUST cover fresh-root bootstrap, stable-gateway reconnect, setup failure and digest mismatch, shell metacharacters in data, symlink traversal, special files, unsafe permissions, interrupted publication, protocol negotiation, request-size limits, and operation receipts.
Tests MUST demonstrate that user values cannot become bootstrap shell syntax and that no operation writes outside approved roots.

### 21.5 Factory validation and planning

Tests MUST cover factory cleanup, secret redaction, retries rejection, label mismatch, distinct bindings, provider adapters, worker initialization, forbidden Parsl initialization, launch-time drift, recursive portable overrides, planning/runtime routing consistency, and proof of zero scheduler/DFK/run/worker allocation.

### 21.6 Submission and reconnection

Tests MUST cover successful planned submission, lost acknowledgements before and after durable allocation, equal resubmission returning one run, uncertain PSI/J acceptance without resubmission, attachment without project files, gateway negotiation, cancellation in every state, retry without original paths, and equivalent attached/reconnected parallel-node diagnostics.

### 21.7 Results and cleanup

Tests MUST cover verified download, interrupted retry, corruption, destination conflicts, idempotent identical destinations, external path preservation, atomic publication, cleanup planning, reference conflicts, active-run protection, retained attachment, transfer leases, explicit run deletion, namespace confinement, and quota diagnostics.

### 21.8 End-to-end acceptance

The authoritative acceptance suite includes:

1. a fresh Slurm account using a locked uv project;
2. a setup script loading Modules or Spack;
3. a locked Pixi scientific environment;
4. an offline wheelhouse with network access disabled;
5. an administrator-managed Python with attestation drift detection;
6. prepare, confirmation, submit, disconnect, attach, progress, cancellation, diagnostics, retry, download, and cleanup using public APIs only.

PBS and LSF require scheduler-adapter integration fixtures and at least one real or hermetic acceptance environment before their capability is reported as supported.

## 22. Implementation sequence

The implementation SHOULD proceed in this order:

1. public frozen types, schemas, and capability reporting;
2. storage-free remote workflow preparation and immutable local ownership;
3. root bootstrap, stable gateway, and operation receipts;
4. uv deployment identity, publication, and exact BioImageFlow bootstrap;
5. uploaded Parsl source, factory runtime, paired bindings, and provider adapters;
6. non-allocating validation and planning using shared runtime routing logic;
7. planned PSI/J submission, idempotency, attachment, and existing run semantics;
8. verified result download and explicit cleanup;
9. Pixi, `pylock.toml`, offline wheelhouse, and existing-Python adapters;
10. documentation replacement and complete acceptance testing.

An implementation phase is complete only when its public reports and failure behavior are documented and its allocation guarantees are tested.

## 23. Explicitly deferred extensions

The following are outside the first implementation:

- resolution from an unlocked bare `pyproject.toml`;
- mutable or unpinned cluster setup sources;
- automatic repository-wide source discovery;
- non-shared or differently mounted worker storage;
- login-node submission relays;
- bootstrap performed inside a scheduler allocation;
- transparent orchestrator failover after allocation loss;
- task-level Parsl-internal drill-down;
- built-in Slurm, PBS, or LSF Parsl configuration generators;
- Poetry, PDM, Conda-lock, Spack-manifest, or container-recipe adapters;
- group-shared writable cluster roots;
- automatic garbage collection during submission.

Each extension requires its own public capability, allocation behavior, security boundary, and conformance tests before it can be enabled.
