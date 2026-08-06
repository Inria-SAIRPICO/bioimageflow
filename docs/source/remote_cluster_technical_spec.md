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
5. A published BioImageFlow installation uses a verified, target-compatible wheel for the exact running version.
   An editable or unreleased checkout is built into a non-editable wheel locally, that exact wheel's digest defines the authoritative artifact, and deployment fails rather than substituting an index copy or rebuilding different bytes on the cluster.
6. uv and Pixi local project members are captured as immutable distribution inputs selected by their build metadata, never by recursively uploading an entire repository.
   A target-compatible wheel is used when preparation can build one, while a native project may instead be captured as an exact source distribution and built in the unpublished cluster candidate under the locked build rules in Section 6.1.
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
It MUST be one non-empty argument, MUST NOT begin with `-`, and MUST NOT contain whitespace, control characters, a URI scheme, or an SSH command or option fragment.
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
ClusterEnvironment.from_uv_project(path, *, groups=(), extras=(), package=None, auth_refs=None)
ClusterEnvironment.from_pixi_project(path, *, environment="default", auth_refs=None)
ClusterEnvironment.from_pylock(lock, *, project=None, groups=(), extras=(), auth_refs=None)
ClusterEnvironment.from_wheelhouse(path, *, lock)
ClusterEnvironment.from_existing_python(path)
```

There is no first-version `from_pyproject()` constructor.
Unrecognized environment formats MUST fail without fallback guessing.

The value records only local source locations and non-secret selection options before preparation.
Its prepared representation replaces local paths with immutable artifact entries and digests.
When a selected source distribution requires a target-side build, the prepared representation also contains the locked build plan defined in Section 6.1.

`auth_refs` maps adapter-declared credential slot names to cluster environment-variable names matching `[A-Za-z_][A-Za-z0-9_]*`.
Each slot binds one canonical index, channel, or artifact-host identity and one adapter-supported credential role; unknown, duplicate, unused, or endpoint-mismatched slots fail before installation.
The mapping and reference names are identity-bearing, while resolved values are late-bound only for bounded artifact retrieval and follow Section 14.

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

The setup digest pins the script bytes, not the mutable Modules, Spack, interpreter, driver, library, or executable targets that those bytes select.
After sourcing the script, BioImageFlow records a bounded setup-realization attestation containing the resolved Python executable and interpreter identity plus only non-secret executable, library, tool-origin, and virtual-package facts required by the selected adapters or workflow.
It MUST NOT infer module or Spack environment identity by parsing arbitrary shell source, treat module names as immutable versions, or persist a general environment dump.
The build-time attestation is retained with the deployment, a fresh login-node attestation is bound into validation and planning, and the observable applicable subset is compared again in the orchestrator and each managed worker.
A changed required observable fact fails with `external-environment-changed`; facts that cannot be observed without allocation remain explicitly unverified until the corresponding runtime acknowledgement.

Setup source bytes and local source paths MUST be absent from `repr()`.
Serialized public values contain source kind, size, and digest, plus a cluster path only for a pinned cluster source.

### 3.4 `SchedulerJob`

`SchedulerJob` describes only the orchestrator allocation.

Its first-version scheduler values are `"slurm"`, `"pbs"`, and `"lsf"`.
Accepting one of those syntax values means only that BioImageFlow implements its normalized job schema; actual support requires the exact scheduler-adapter and PSI/J compatibility checks in Sections 10.2 and 19.

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
Package snapshots MUST use an explicit relative-file manifest and reject symlinks, hard links, special files, path aliases, mutation during capture, and configured file-count or byte limits.

An advanced `from_module("module:function", ...)` constructor MAY refer to code already provided by an externally managed environment.
Its module distribution and observed environment attestation become plan-critical external claims.

`kwargs` contains finite JSON-safe values.
`secret_refs` maps Python-identifier factory argument names to cluster environment-variable names matching `[A-Za-z_][A-Za-z0-9_]*` in the first implementation.
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

These restrictions are a trusted-code contract, not behavior BioImageFlow can prove for arbitrary Python.
BioImageFlow MUST prevent its own validation runtime from creating a run, DataFlowKernel, or scheduler submission and MUST detect supported Parsl initialization paths where practical, but a factory that reaches an undeclared scheduler client or subprocess is outside the managed contract.

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

`ClusterDeployment` is a receipt for remote retained state, not an owner of laptop files or a deletion capability.
`PreparedClusterInvocation` owns its local snapshot, and one active `RemoteExecutionPlan` at a time holds an exclusive lease that prevents those bytes from being closed or expired while needed.
`RemoteWorkflowRun` owns no laptop files; after remote allocation it refers only to the gateway's durable run record and retained remote objects.

When `from_dict()` reconstructs a value whose public form intentionally omits private source locations, owned bytes, a cluster client, or another required live resource, the result is a detached summary rather than an operational owner.
A detached value remains inspectable and serializable, but an operation requiring omitted state MUST fail with `local-state-unavailable` instead of consulting an original path or guessing how to recreate the state.
After a plan has begun remote mutation, recovery from its detached summary uses `cluster.attach(summary.run_id)`; before mutation, the caller must retain the live prepared invocation and plan or create new ones.

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

The existing root itself MUST be a real directory owned by the effective SSH user and MUST NOT be group- or world-writable.
Creation of a missing root MUST open and validate its existing parent before creating the final directory with `0700`; an existing symlink or non-directory at the requested root fails.
Once validated, every descendant operation MUST be relative to an open directory handle, or use an equivalently race-resistant mechanism, with no check-then-use path resolution.
Regular files that carry executable code, control state, receipts, manifests, or immutable objects MUST have a link count of one when published and whenever reused.

The gateway MUST reject a symlink in any managed path component, an existing special file, unexpected ownership, group- or world-writable control or deployment directories, and hard-link or rename operations that escape a managed namespace.
It MUST compare the opened object's device, inode, type, owner, mode, and expected identity with the inventory or manifest immediately before publication, reuse, or deletion.

New private directories use mode `0700`, regular control files use `0600`, and executable wrappers use `0700`, subject only to a more restrictive filesystem policy.
Remote processes run as the SSH user and MUST NOT request privilege, change ownership, or weaken permissions.

Atomic publication and receipt updates MUST use a temporary sibling in the same managed namespace, flush file contents and the containing directory before acknowledgement, and never overwrite an unequal published identity.

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
Managed uv and `pylock.toml` environments use an explicitly attested compatible interpreter made visible by setup and MUST NOT trigger an implicit Python download; a Pixi lock may instead supply its selected Python package.
These are site prerequisites, not packages BioImageFlow attempts to install with privilege.

### 5.2 Bootstrap boundary

The laptop package contains or constructs a versioned bootstrap artifact with a published digest and protocol range.

The authoritative bootstrap and gateway bytes are the immutable resources in the exact local BioImageFlow distribution, or artifacts built from the exact snapshotted local checkout for an unreleased version.
Bootstrap MUST NOT fetch executable control-plane code by version name or accept a server-provided replacement artifact.
The client verifies its snapshot against the local distribution manifest when one exists, records its computed digest before contact, and requires the remote candidate to match that digest.
These digests detect accidental or account-local modification; authenticity relies on the user's local BioImageFlow installation and ordinary SSH host-key verification, and compromise of either is outside this trust boundary.

Bootstrap uses system OpenSSH and SFTP with argument arrays and `shell=False` locally.
It honors the user's ordinary SSH configuration, agent, jump hosts, and host-key policy.
It MUST NOT accept passwords, private-key bytes, arbitrary SSH option strings, or host-key bypass values.
All library-selected SSH options precede the validated destination, the destination remains exactly one argument, and the remote command is one library-owned constant argument with no user-derived text.
Timeouts and other supported connection settings MUST be range-checked scalars rendered by BioImageFlow rather than forwarded strings.
SFTP MUST use the same destination and connection policy, and every command operand MUST be either a client-generated safe local temporary name or the confined server-issued path whose components use a fixed safe-token grammar.
No user value may be inserted into an SFTP batch-language command.

Before a gateway exists, the only permitted remote command is a library-owned constant Bash bootstrap program.
User values are transferred through a length-delimited stdin envelope or safe encoded data fields, never interpolated as executable shell text.

Bootstrap is a resumable sequence of bounded invocations rather than one long-lived remote process.
The first invocation probes or creates the root and returns a server-generated upload token, SFTP transfers only to the corresponding partial path, and a later invocation verifies and publishes the candidate.
Each bootstrap mutation carries an operation ID and request digest in a minimal bootstrap receipt namespace so a lost acknowledgement repeats the same step instead of creating another candidate.
`deploy()` and the deployment phase of `cluster.submit()` snapshot the setup source, gateway artifact, BioImageFlow artifact, and all other local deployment inputs before the first bootstrap invocation.

The bootstrap program may, across those invocations:

1. validate or create the dedicated root with private permissions;
2. allocate a private server-named partial upload location;
3. authorize bootstrap artifact transfer to that partial location through SFTP;
4. verify sizes, SHA-256 digests, file types, and confined paths;
5. verify and source the setup script;
6. invoke the discovered Python to install a versioned control-plane gateway candidate that is independent of any workflow deployment;
7. atomically publish that gateway;
8. return a canonical JSON capability response.

It MUST NOT submit a scheduler job, install outside `root`, invoke `sudo`, modify shell startup files, or install workflow dependencies.
It MUST run with a fixed non-interactive Bash entry, explicitly source only the verified setup candidate, and never evaluate request data with `eval`, command substitution, shell expansion, or generated shell source.

### 5.3 Stable gateway entry

After bootstrap, `<root>/gateway/entry` is the stable remote command used by `RemoteCluster`.
It is a small cluster-root-owned dispatcher, not a daemon.
SSH invokes it once per bounded request and it exits after one response.

Each immutable gateway publication binds its bootstrap setup digest, Python identity, executable environment, gateway artifact digest, and supported protocols.
The stable entry sources the gateway-owned verified setup copy when necessary and invokes the gateway-owned interpreter by absolute path.
It does not depend on the caller retaining a `SetupScript` object or rerun environment discovery during attachment.

The dispatcher selects an immutable compatible gateway implementation by protocol version or retained run metadata.
A gateway publication has a stable publication ID and manifest digest, and the dispatcher MUST verify its manifest, interpreter, setup copy, entry point, and content digests before every selection.
For a retained run, selection MUST use the exact gateway publication recorded by that run unless a declared protocol adapter with an identical run-operation contract is explicitly recorded for that publication.
The laptop accepts a selected gateway only when its version, protocol range, and artifact digest match either the gateway artifact in the current local distribution or an immutable compatibility-catalog entry shipped in that distribution.
Compatibility by version string or protocol number alone is insufficient, and an unknown digest fails with `gateway-untrusted` without mutation.
A guarded gateway upgrade MAY atomically update dispatcher metadata, but MUST NOT modify an immutable gateway or change the deployment bound to an existing run.
Only a configuration-complete `deploy()` or `cluster.submit()` MAY bootstrap or upgrade the gateway; `check_connection()`, `attach()`, and run operations MUST NOT do so implicitly.

This stable entry is what permits `RemoteCluster(host=..., root=...).attach(run_id)` without the original project files or setup object.

### 5.4 Gateway protocol

Requests use `bioimageflow.cluster.request.v1` and responses use `bioimageflow.cluster.response.v1`.

Each request contains protocol version, UUID4 request ID, operation name, canonical JSON-safe arguments, and a payload digest when uploaded bytes are referenced.
Each response contains the matching request ID, success or failure status, structured payload or sanitized diagnostic, and gateway and supported protocol versions.

Mutating operations also carry a stable operation ID and canonical request digest.
The gateway persists an operation receipt before acknowledging a completed mutation.
Repeating an equal operation returns the same logical result, while reusing an operation ID with different bytes fails with `operation-conflict`.
Receipts are gateway-created `0600` records that bind operation name, canonical request digest, phase, affected identities, result or diagnostic digest, and monotonically guarded revision.
The gateway MUST durably publish and re-read a receipt before acknowledging the mutation and MUST NOT accept an uploaded or client-authored receipt as authority.
A missing, malformed, permission-unsafe, or digest-inconsistent receipt after a mutation may have occurred fails closed with `operation-record-tampered`; it MUST NOT be deleted, reconstructed from client claims, or treated as proof that retrying a scheduler submission is safe.

Uploads use only server-issued partial paths.
Publication verifies the complete manifest and atomically renames a confined candidate.
Client-supplied absolute upload destinations are forbidden.

### 5.5 Upload and archive validation

Before accepting bytes, the gateway MUST validate a canonical manifest whose declared aggregate size, entry count, per-entry size, logical path lengths, and nesting depth are within its advertised limits.
Logical entry names are normalized relative POSIX paths and MUST reject absolute paths, empty or dot components, `..`, backslashes, platform drive prefixes, duplicate names, Unicode normalization aliases, and file/directory prefix conflicts.

Each upload is written to a newly created regular partial file with `0600`, no followed links, and an enforced byte ceiling.
The gateway MUST reject early EOF, trailing bytes, digest or size mismatch, link-count changes, and replacement during verification.
Only a fully verified object may enter the immutable object namespace, and a failed or interrupted partial is never considered an object or executable input.

Every BioImageFlow-managed ZIP, tar archive, source bundle, result bundle, or wheel inspected outside a package manager MUST be validated before extraction.
Validation MUST reject encrypted or multi-volume archives, absolute or escaping member names, duplicate or aliased members, symlink, hard-link, device, FIFO, socket, sparse, or unsupported member types, and any archive whose compressed bytes, expanded bytes, entry count, path length, nesting depth, or compression ratio exceeds an advertised limit.
Extraction occurs only into a new private candidate using race-resistant relative operations, applies normalized private modes rather than archived ownership or permission metadata, and publishes only after the extracted manifest matches the expected digest set.

Package build hooks and installed package code remain trusted executable code, but their input archives still require the hashes and format validation promised by the selected environment adapter.

## 6. Environment preparation

### 6.1 Common rules

Every managed environment MUST include compatible exact versions of BioImageFlow, Parsl, PSI/J, the selected PSI/J scheduler plugin, the workflow's orchestrator-side packages, and packages promised by managed executor bindings.

The submitted client BioImageFlow artifact is authoritative.
If an environment lock resolves an incompatible BioImageFlow version, deployment fails with `bioimageflow-version-conflict` rather than silently selecting one side.

Every selected local project is captured before deployment as either an exact target-compatible wheel or an exact source distribution produced through its declared build backend.
Editable installs, direct installation from local directories, and mutable source-tree imports are forbidden in a published deployment.

Each environment adapter MUST select exact installer and build-frontend artifacts compatible with the bootstrap interpreter and target.
Their tool identities, versions, artifact filenames, sizes, SHA-256 digests, and retrieval endpoint identities are prepared inputs; an unpinned `latest` tool, ambient installer, or installer-selected build tool MUST NOT be used.
The artifacts are uploaded or retrieved only through the hash-verified artifact rules for that adapter.

Before any source distribution is built on the cluster, the adapter MUST produce a locked target build plan.
The plan binds the source-distribution digest, target operating system, architecture, Python implementation and ABI, build-backend entry point, backend path and configuration settings, exact installer and build-frontend artifacts, and the complete isolated build-requirement closure as distribution names, versions, artifact filenames, sizes, SHA-256 digests, and non-secret endpoint identities.
Requirements returned by a PEP 517 dynamic build hook MUST already match entries in that closure; a hook MUST NOT add, resolve, or download an unlisted requirement.
The gateway builds in a new private isolated environment containing only that closure, with dependency resolution and implicit network retrieval disabled, then validates the resulting wheel and records its filename, size, tags, and SHA-256 digest before installation.
A missing, unhashed, incompatible, or additional build requirement fails with `environment-build-lock-incomplete`, and the candidate is never published.

Installer and build-tool artifacts, locked build plans, build output, selected groups or extras, target platform, lock data, setup-realization facts used by the build, and artifact digests are identity-bearing.
Because a target-built wheel digest is known only after the isolated build, preparation has a build-input key while `cluster.deploy()` returns the final deployment ID after recording that digest; no plan may refer to the candidate or build-input key as a deployment ID.
Package installation is non-interactive and MUST NOT update a supplied lock.

### 6.2 uv projects

`from_uv_project()` requires a `pyproject.toml` and an up-to-date `uv.lock` in the selected project root.

The selected groups, extras, package, workspace members, indexes by non-secret identity, and target platform are explicit prepared inputs.
The lock MUST be installed with frozen semantics.

Local workspace members selected by the resolution are built through their declared build backends into immutable distributions.
Files included by those build artifacts are captured; unrelated repository files are not uploaded.
An unpackageable local dependency fails before remote installation.
Any member sent as a source distribution MUST have a complete locked target build plan derived from the uv project and lock inputs under Section 6.1.
If uv cannot select the required target build closure without changing `uv.lock`, preparation fails instead of resolving it during deployment.

### 6.3 Pixi projects

`from_pixi_project()` requires `pixi.toml` or a Pixi-enabled `pyproject.toml`, an up-to-date `pixi.lock`, and a named environment present in that lock.

The selected environment MUST contain a target matching the observed cluster platform.
Installation uses locked or frozen Pixi semantics and MUST NOT re-solve on the cluster.

Preparation records the manifest-declared ordered channel set, channel priority mode, canonical non-secret channel identities, and any PyPI index identities that contributed to the selected lock environment.
It MUST reject a lock created under a different channel configuration, undeclared channels supplied by user or system Pixi configuration, and channel or index redirection to an identity not bound by the prepared inputs.
Credentials may vary only through matching `auth_refs`; they MUST NOT change endpoint identity or package selection.

Preparation also records the exact target platform and normalized virtual-package assumptions used by the selected lock, including version or build constraints for facts such as the operating system, libc, and CUDA when present.
After the setup script, deployment MUST compare every adapter-observable assumption with the login-node realization before installation; an unsatisfied assumption fails with `environment-platform-incompatible`.
Validation binds a fresh observation, the orchestrator repeats the applicable comparison before Parsl initialization, and each managed worker repeats the applicable subset before accepting tasks.
An assumption that cannot be observed in a given non-allocating context MUST be reported as unverified rather than satisfied.

A compatible Pixi bootstrap artifact is verified and installed under the cluster root when required.
The Pixi bootstrap artifact and all Conda and PyPI artifacts selected by Pixi are pinned by version, filename, size, SHA-256 digest, and canonical non-secret endpoint identity in the deployment manifest.

### 6.4 Standard Python locks

`from_pylock()` accepts a PEP 751 `pylock.toml` supported by the selected installer.
The selected dependency groups, extras, environment markers, Python requirement, artifact URLs or local artifacts, sizes, and hashes are validated before publication.

Mutable VCS references, unhashed archives, or unsupported local-directory entries fail unless preparation converts them into an immutable built artifact.
If any selected project or dependency requires a target-side build, preparation MUST derive the complete locked build-requirement closure required by Section 6.1 from the lock and immutable project inputs.
When the supplied `pylock.toml` cannot represent that closure, or the adapter cannot prove it without resolution, preparation fails with `environment-build-lock-incomplete`; the user must supply target-compatible wheels or choose a supported lock adapter that locks the build closure.

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
- setup source kind and digest plus the build-time setup-realization attestation;
- environment kind, selections, manifest and lock digests, resolved package set, channel and index identities, target and virtual-package assumptions, and artifact digests;
- exact BioImageFlow bootstrap distribution and gateway compatibility digests;
- Parsl source bundle digest, factory name, JSON-safe arguments, and secret reference names;
- generated factory-runtime contract version;
- selected scheduler and managed provider-adapter identities and versions;
- exact installer, build-frontend, and build-requirement artifact identities and digests;
- locked target build plans and selected local project source-distribution and wheel digests;
- target-built wheel filenames, tags, sizes, and digests.

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

Concurrent attempts with no target-side builds coordinate directly by deployment ID.
Attempts requiring target-side builds first coordinate by the canonical build-input key and operation ID, then compute and publish under the final deployment ID after recording every build output digest.
They either reuse a verified published deployment or completed equal build receipt, wait for the active installer, or replace a provably abandoned candidate.
They MUST NOT merge partial files.

Deployment upload, build, and publication are journaled by build-input key when present, final deployment ID when known, and operation ID.
A repeated equal `deploy()` resumes verified completed transfers, reuses a published deployment, or discards an abandoned mutable installation candidate and starts a fresh candidate; it never continues an installer inside a partially mutated environment.

Every published content-owned deployment is made read-only and verified against its manifest before reuse.
A mismatch reports `deployment-tampered`; BioImageFlow MUST NOT repair it in place.
The same fail-closed rule applies to immutable gateways, objects, run records, indexes, and operation receipts: the gateway may remove an unreferenced partial candidate, but MUST NOT silently repair, replace, quarantine, or delete corrupted published state.
When corruption affects allocation or scheduler certainty, diagnostics report allocation state `unknown` and retry safety `unsafe` or `same-attempt-only` as applicable.

`cluster.deploy()` creates no workflow run, initializes no DataFlowKernel, allocates no worker, and submits no scheduler job.

## 8. Parsl resolution, bindings, and worker startup

### 8.1 Isolated factory validation

Factory import and invocation occur in a short-lived child process inside the selected deployment.
The process uses a bounded timeout, a private temporary directory, a minimal inherited environment, and resolved secret references.
It is isolation for cleanup and diagnostics, not a security sandbox for untrusted code.
Resolved values MUST reach the child through a private bounded handoff or pipe, never command-line arguments, process titles, generated source, or inherited variables other than the explicitly implemented secret channel.

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
When secrets are present, arbitrary child output MUST NOT enter a public diagnostic unless it has passed the bounded redaction policy in Section 14; otherwise the public diagnostic uses a stable generic message and points to a private restricted log, if one is retained.

### 8.2 Managed provider adapters

Each supported executor or provider adapter is versioned and declares compatible Parsl package versions and concrete public types, the provider's worker-scheduler family, compatible orchestrator scheduler families, how to read the public worker-initialization field, how to compare it with `runtime.worker_init`, which provider settings can be normalized safely, and whether the provider supports the required shared-root and nested-submission topology.

Provider support is evaluated for the exact tuple of Parsl version, executor type, provider type, provider-adapter version, worker-scheduler family, orchestrator scheduler family, and normalized topology claims.
Matching a class name, accepting a scheduler string, or finding an importable provider package alone MUST NOT be reported as support.

Unknown or incompatible types fail with `unsupported-managed-provider`.
The implementation MUST NOT fall back to private attribute probing or assume worker initialization was applied.

An advanced external binding MAY bypass managed initialization only when it explicitly declares an externally managed worker environment and passes the existing origin, core-version, storage, and resource checks.

### 8.3 Launch-time comparison

The orchestrator invokes the same factory again immediately before initializing Parsl.
BioImageFlow normalizes its safety-relevant result and compares it with the confirmed plan.

The comparison includes executor labels, bindings, worker initialization, retry policy, environment identities, storage mode, managed tool origins, slot capacities, and supported normalized provider settings.
A difference fails the run before DataFlowKernel creation with `parsl-configuration-changed`.

### 8.4 Worker startup acknowledgement

Before a managed executor route accepts workflow tasks, at least one worker on that route MUST acknowledge the exact deployment ID, setup and activation marker, applicable setup-realization and virtual-package facts, BioImageFlow core compatibility, shared storage visibility, tool-origin availability, observable declared devices, and connectivity to the orchestrator.

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
Closing or expiry while no plan lease exists removes only its private local copies.

A successful `plan()` call gives the returned plan an exclusive submission lease on the snapshot without duplicating its bytes.
A planning failure acquires no lease, and closing an unsubmitted plan releases its lease so the same immutable preparation can be replanned after expiry or rejection.
One prepared invocation can seed only one active plan at a time; each plan still defines a distinct stable attempt and run ID.

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
It writes no remote file, invokes no caller-supplied setup or factory code, and submits no job.
An installed stable gateway MAY source the immutable setup copy already bound to that gateway, because some gateway interpreters cannot start without site initialization.

On a fresh root it reports that bootstrap is required rather than bootstrapping implicitly.

### 10.2 Remote validation

`cluster.validate(deployment=...)` requires a published deployment.

It validates gateway and deployment integrity, setup realization and Python invocation, environment or external attestation, trusted Parsl factory behavior, referenced secrets, `retries=0`, executor labels and bindings, managed provider adapters, PSI/J support, paths, launch values, and shared-storage declarations.

Scheduler validation MUST resolve the requested normalized scheduler to one named BioImageFlow scheduler-adapter version and one PSI/J executor descriptor in the exact installed PSI/J core and scheduler-plugin versions.
For each managed route it MUST then evaluate the provider-support tuple from Section 8.2 and report separately whether the orchestrator adapter is implemented, the PSI/J descriptor is installed and compatible, the provider adapter is implemented and compatible, and shared-root or nested-submission claims remain runtime-unverified.
An unsupported orchestrator combination fails with `unsupported-scheduler-adapter`; an unsupported managed worker combination fails with `unsupported-managed-provider`.

BioImageFlow creates no workflow run, initializes no DataFlowKernel, allocates no worker, and submits no scheduler job during validation.
Setup scripts and Parsl factories remain trusted code whose undeclared external side effects are outside that guarantee, as described in Section 3.6.

`ClusterValidationReport` has schema `bioimageflow.cluster_validation_report.v1` and contains deployment ID, setup-realization and environment attestation digests, normalized bindings, the per-orchestrator and per-route scheduler/provider evidence above, verified/declared/unverified facts, sanitized diagnostics, validity and expiry metadata, and relevant gateway, adapter, runtime, plugin, and protocol versions.

The report MUST state that compute-node mounts, worker networking, nested submission policy, queue availability, future quotas, and hardware cannot be proven without allocation.

### 10.3 Planning

`cluster.plan(prepared, deployment=..., validation=None)` returns `RemoteExecutionPlan`.

When `validation` is omitted or stale, planning MAY perform the same bounded remote validation.
It MUST NOT deploy, install, create a run, submit a job, initialize a DataFlowKernel, or allocate a worker through any BioImageFlow-controlled path.

Planning uses the same recursive scope compilation, effective node requirements, environment compatibility, storage compatibility, tool-origin compatibility, executor capacity comparison, task policy, cache selection, and route selection as runtime dispatch.

For every scoped `ProcessingTool` node it returns scoped node path, effective resources, compatible executor labels, selected route and reason, environment/origin/storage compatibility, cache status, and structured incompatibility reasons.
`DataFrameTool` nodes remain visible as orchestrator work and expose no worker resource override.

The plan binds the cluster destination (`host` and normalized `root`), prepared invocation digest, deployment and external attestation, validation evidence and expiry, normalized factory claims, routing and cache revision, scheduler job, remote storage, stable submission attempt ID, and preallocated run ID.
Its schema is `bioimageflow.remote_execution_plan.v1`.
Its plan digest is the SHA-256 digest of the canonical public plan payload with the `plan_digest` field omitted; private owned bytes and live clients are never digest inputs.

### 10.4 Plan lifetime

A plan has a finite expiry.
Expiry never changes its recorded meaning, but prevents submission until the user creates and confirms a new plan.

The plan holds an exclusive lease on the prepared local bytes needed for submission and owns a private local recovery record for its stable attempt and run IDs.
Its public confirmation summary is JSON-safe, but deserializing that summary alone does not recreate missing local owned bytes.

## 11. Submission and idempotency

### 11.1 Explicit submission

`plan.submit()` is the confirmation boundary.

It MUST verify local prepared bytes and plan digest, verify the exact deployment, and revalidate external and plan-critical claims before its first mutation.
Its first remote mutation durably allocates the preselected run and attempt IDs and installs an attachable run record before any upload or scheduler action.
It then uploads only prepared objects, binds their durable references to that run, persists submission and scheduler intent, submits one PSI/J orchestrator job, persists its correlated receipt when available, and returns `RemoteWorkflowRun`.

It MUST NOT reread original workflow, configuration, setup, project, or input paths.
It MUST NOT silently regenerate routes or select a different deployment.

### 11.2 Convenient submission

Before its first network operation, `cluster.submit(workflow, ...)` snapshots all local environment, setup, Parsl, workflow, tool-source, and input bytes needed by the composed operation.
It then performs deployment publication or reuse, validation, planning, and `plan.submit()` from those snapshots.
Failure during the initial local snapshot creates no remote state.
No later phase rereads the original paths.

It creates one plan and one stable attempt.
An internal retry caused by a lost acknowledgement reuses that attempt and MUST NOT prepare or plan again.

The method MAY expose structured progress callbacks for deployment and upload phases.
Those callbacks do not change semantics.

### 11.3 Operation receipts

The submission attempt ID is generated before the first mutating remote request.
All submission mutations are journaled under that ID.

The durable attempt phases are `allocated`, `uploading`, `ready`, `scheduler-intent`, `submitted`, `rejected`, `cancelled`, and `uncertain`.
Every phase transition records the exact plan digest, run ID, deployment ID, invocation digest, and referenced object identities before acknowledging the transition.

Repeating `plan.submit()` returns the original run when accepted, resumes a safe incomplete transfer or pre-submit phase, reconnects to a known scheduler receipt, or reports `submission-uncertain` without resubmitting when scheduler acceptance cannot be disproved.
It never creates a second run ID or scheduler job.
After pre-submission cancellation has durably moved the attempt to `cancelled`, repeating `plan.submit()` returns the original cancelled run without resuming uploads or submitting a job.

Once the first mutating request is sent, the plan is sealed to its preallocated run and attempt IDs rather than becoming generally consumed.
The plan MUST retain its verified local prepared bytes while they are required to resume an incomplete upload or recover an acknowledgement.
`close()` MAY release the plan's snapshot lease only after the gateway confirms that all required objects and run references are durable, or after cancellation of the allocated run durably reaches `cancelled` before `scheduler-intent` and the gateway confirms that no scheduler submission is possible.
Closing a sealed plan without either proof MUST preserve its snapshot lease and recoverable local attempt record or fail with a structured `attempt-still-uncertain` diagnostic.
No ownership of laptop bytes transfers to `RemoteWorkflowRun`; the run becomes independently attachable when its remote record is allocated, and independently executable when its retained object references are complete.

`RemoteSubmissionUncertainError` contains host, root, run ID, attempt ID, stable category, sanitized message, and `next_action`.
It contains no secret value or live remote object.

### 11.4 Orchestrator launch

PSI/J launches only the BioImageFlow orchestrator.
Parsl providers inside that orchestrator request worker allocations.

The PSI/J job uses the exact deployment Python, verified setup copy, remote workflow storage, and run-owned submission.
It never receives an original mutable setup path or mutable deployment alias.

The orchestrator revalidates factory claims before Parsl initialization and records its exact launch environment in run provenance.

Immediately before PSI/J submission, the gateway resolves every environment-variable secret reference and writes the values to a run-private `0600` handoff file outside identity-bearing manifests and operation receipts.
The PSI/J job receives only the confined handoff path, and the deployment-owned launch wrapper reads and unlinks the file before invoking user code.
The gateway MUST remove an unclaimed handoff after definite scheduler rejection, and cleanup MUST treat a handoff for an accepted or uncertain submission as run-owned protected state.
The orchestrator fails with `secret-reference-missing` if the handoff is absent or malformed and MUST NOT write its contents to logs or durable metadata.

## 12. Run state, observation, and control

### 12.1 Run states

The durable run states remain `prepared`, `starting`, `running`, `finalizing`, `cancel_requested`, `succeeded`, `failed`, `cancelled`, and `lost`.

State changes use guarded revisions and claim epochs.
Only terminal success, failure, cancellation, or loss may be the parent of a `RunRetryPlan` or `start_retry()` operation.

The run binds the exact cluster root, storage path, deployment ID, invocation digest, execution-plan digest, attempt ID, scheduler intent, and scheduler receipt.

### 12.2 Attachment

`cluster.attach(run_id)` requires only host, root, connection policy, and run ID.
It MUST NOT require the original workflow, environment definition, setup source, Parsl source, or local prepared bytes.

The gateway resolves the run ID through the confined `<root>/runs` index and verifies the indexed storage, deployment, invocation, attempt, and gateway bindings before returning an observation.
It MUST NOT search arbitrary result roots, infer storage from the run ID, or reconstruct a missing run record.
Attachment to an allocated but not yet submitted run reports its durable attempt phase and recovery guidance; it does not resume an upload without the originating plan's owned bytes.

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

For an allocated run whose attempt has not reached `scheduler-intent`, cancellation MUST atomically prevent later submission, move the attempt to `cancelled`, and move the run to `cancelled` without contacting the scheduler.
That durable confirmation permits the originating plan to release its snapshot lease.

Cooperative cancellation is attempted first.
Scheduler cancellation uses the retained PSI/J receipt after the configured grace period.
Confirmed forced termination records `lost` when worker and writer cleanup cannot be proven.

Calling `cancel()` on a terminal run returns `None`, refreshes the handle to the unchanged terminal observation, and creates no error or new action.

### 12.5 Retry

The current public `RunRetryPlan` and recomputation semantics remain authoritative.
A remote retry reuses the retained deployment, invocation, input objects, and setup material after digest verification.
It never reads original laptop paths.

If the retained external Python attestation no longer matches, retry planning reports incompatibility and cannot produce a `RunRetryPlan` for that retained submission.
Selecting a different deployment starts a new top-level submission rather than changing or continuing a retry plan, because `RunRetryPlan` clones the exact retained submission.

## 13. Results and downloads

A successful run installs its exact public return before entering `succeeded`.
Historical result loading never consults mutable current-cache pointers.

`run.download_result(destination)` is the `RemoteCluster` name for the current `RemoteWorkflowRun.export_result(destination)` contract: it prepares and downloads the existing verified portable result bundle and returns the rehydrated typed public result.

It MUST bind the exact run and return digest, include DataFrames and owned explanatory assets, retain declared external cluster paths as typed references, transfer through a private sibling, verify every entry, publish atomically, reject unrelated destinations, accept identical destinations idempotently, and safely retry interrupted transfer.

Preparing a result transfer creates no workflow run and submits no scheduler job.
Temporary transfer objects are protected while a client lease or retained download receipt references them.

## 14. Secrets and trusted code

Secret values are late-bound external inputs and never contribute to deployment, invocation, or plan digests.
Secret reference names and their argument mapping are identity-bearing.

BioImageFlow MUST redact values it resolves from manifests, schemas, public representations, structured diagnostics and events, controlled logs, child-process exception payloads, and operation receipts.
Missing secrets are reported only by reference name.

Secret resolution is bounded by advertised per-value and aggregate byte limits, rejects NUL-containing values, and occurs only for the named operation immediately before the trusted process needs the values.
Values MUST NOT appear in SSH, SFTP, PSI/J, scheduler, or child-process command arguments, generated source, identity inputs, or durable request and receipt bodies.
Private handoff files are created without replacement, confined to the owning operation or run, and removed after successful read or definite non-use as specified in Section 11.4.

Redaction of BioImageFlow-controlled text MUST replace exact resolved byte sequences before persistence or return and MUST bound captured output before redaction.
BioImageFlow cannot reliably identify transformed, encoded, fragmented, derived, or independently reread secret material emitted by trusted code; callers MUST treat setup, factory, build, workflow, and scheduler-wrapper output as potentially sensitive.
Implementations MUST NOT describe redaction as protection against deliberate exfiltration by trusted executable code.

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
Deletion MUST use the already validated namespace directory handle, compare the target's device, inode, type, owner, mode, link count, identity digest, and reference revision with the plan, and refuse mount points, symlinks, special files, or unmanifested descendants.
After revalidation, the gateway first atomically renames the exact candidate to a private deletion tombstone in the same namespace and durably removes its index reference; resumable unlinking then operates only beneath that tombstone without following links.

Cleanup MUST refuse to remove a deployment referenced by an active run, an object referenced by a retained run or retry plan, a transfer with an active lease, or a run record not explicitly selected for terminal-run deletion.

Terminal-run deletion records the loss of attachment, diagnostics, retry, and possibly result availability.
Cleanup accepts no recursive arbitrary path supplied by the user.
All deletion targets come from gateway inventory and remain confined to managed namespaces.
Cleanup MUST protect the stable gateway entry, every gateway publication required by a retained run or recoverable operation, and operation receipts whose absence could make submission retry safety uncertain.

### 15.3 Quota diagnostics

Deployment, upload, and submission estimate required bytes and inode counts when possible.
Failures use stable `quota-bytes`, `quota-inodes`, or `insufficient-space` categories and preserve safely reusable completed objects.

### 15.4 Resource and denial-of-service bounds

Gateway capabilities MUST report hard limits for canonical request bytes and nesting, manifest entries, logical path length and depth, single and aggregate upload bytes, archive expansion and compression ratio, captured subprocess output, validation time and child processes, concurrent transfers and mutations, retained temporary bytes, and inventory page size.
Every parser and operation MUST enforce the applicable limit incrementally, before durable allocation when possible, and return `resource-limit-exceeded` without publishing partial state.
Unknown-length streams, decompression, hashing, diagnostics capture, and recursive inventory MUST remain subject to byte, entry, time, and depth ceilings rather than relying only on declared sizes.
Per-root concurrency controls MUST be bounded and fair enough that repeated probes or abandoned uploads cannot indefinitely block attachment, cancellation, or receipt recovery.
These limits protect one user-owned root from accidental or hostile inputs processed through its gateway; they do not provide tenant isolation from other code running as the same cluster account.

## 16. Operation effects

This table describes effects initiated by BioImageFlow itself.
Setup scripts, package build hooks, and Parsl factories are trusted executable code and can violate the declared contract; BioImageFlow rejects detected violations on supported paths but cannot prove the absence of arbitrary undeclared side effects.
The trusted-code column lists operation-specific user code after gateway startup; any cluster-contacting operation MAY first source the immutable setup copy bound to the selected gateway as stated in Section 5.3.

| Operation | Contacts cluster | Writes remote state | Runs trusted user code | Submits scheduler job | Creates run | Allocates workers |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `check_connection()` | Yes | No | No | No | No | No |
| `deploy()` | Yes | When absent | Setup and package builds | No | No | No |
| `prepare()` | No | No | Workflow serialization only | No | No | No |
| `validate()` | Yes | Temporary bounded state only | Setup and Parsl factory | No | No | No |
| `plan()` | Maybe | Temporary bounded state if validation refreshes | Factory only if validation refreshes | No | No | No |
| `plan.submit()` | Yes | Yes | Setup, factory, orchestrator | Yes | Yes | Later through Parsl |
| `cluster.submit()` | Yes | Yes | All composed phases | Yes | Yes | Later through Parsl |
| `attach()` and inspection | Yes | No | No | No | No | No |
| `cancel()` | Yes | Yes | No new code | May cancel retained jobs | No new run | No new workers |
| `download_result()` | Yes | Bounded transfer state | No | No | No | No |
| `plan_cleanup()` | Yes | No | No | No | No | No |
| `apply_cleanup()` | Yes | Deletes selected state | No | No | No | No |

Temporary files created by validation are not durable remote state and MUST be removed before the operation returns, including on failure or timeout.
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
- `configuration-incomplete`;
- `local-state-unavailable`;
- `setup-failed`;
- `setup-digest-mismatch`;
- `environment-lock-invalid`;
- `environment-build-lock-incomplete`;
- `environment-artifact-missing`;
- `environment-platform-incompatible`;
- `bioimageflow-version-conflict`;
- `deployment-install-failed`;
- `deployment-tampered`;
- `gateway-untrusted`;
- `operation-record-tampered`;
- `external-environment-changed`;
- `secret-reference-missing`;
- `parsl-factory-failed`;
- `parsl-retries-enabled`;
- `executor-label-mismatch`;
- `unsupported-scheduler-adapter`;
- `unsupported-managed-provider`;
- `worker-initialization-missing`;
- `route-incompatible`;
- `validation-expired`;
- `plan-expired`;
- `parsl-configuration-changed`;
- `scheduler-rejected`;
- `submission-uncertain`;
- `attempt-still-uncertain`;
- `worker-startup-failed`;
- `protocol-incompatible`;
- `operation-conflict`;
- `resource-limit-exceeded`;
- `quota-bytes`;
- `quota-inodes`;
- `insufficient-space`;
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
  "host": "alice@login.example.edu",
  "cluster_root": "/cluster/project/alice/bioimageflow",
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
A deserialized summary is suitable for persistence, display, comparison, and recovery through `cluster.attach(run_id)` after remote mutation begins, but it cannot call `submit()` or recover an unallocated attempt.

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

Laptop capability inspection reports only client implementation availability and MUST NOT claim that a scheduler or provider is installed or usable on an uncontacted cluster.
Gateway capability discovery reports protocol and adapter schema versions, not support for a concrete deployment runtime.
Only `ClusterValidationReport` may report a requested scheduler/provider combination as validated, and it MUST key that result by the exact scheduler-adapter, PSI/J core and executor-plugin, Parsl, executor, provider, and provider-adapter identities and versions described in Section 10.2.
Queue availability, nested scheduler submission, service-node policy, compute-node storage visibility, and worker hardware remain unverified until the operation that can observe them; they MUST NOT be collapsed into a boolean `supported` capability.

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

Tests MUST cover strict construction and round trips for public JSON-safe values, detached-summary behavior when private state is omitted, `local-state-unavailable` failures without original-path reads, secret rejection and redaction, normalized paths/scheduler values/resources, complete registration of every normative diagnostic category, and optional dependency inspection without Parsl or PSI/J imports.

### 21.2 Preparation and identity

Tests MUST prove immutable local snapshots, stable and sensitive deployment identity, exclusion of timestamps and secret values, non-editable wheel creation, target-side source-distribution builds with pinned build closures and output digests, bounded uv/Pixi source capture, tamper rejection, and concurrent equal deployment publication through build-input and final deployment identities.

### 21.3 Environment adapters

Tests MUST cover successful and failing fixtures for locked uv, locked Pixi, `pylock.toml`, offline wheelhouse, and existing Python.
They MUST include stale locks, missing or additional build requirements, dynamic build-hook requirements outside the locked closure, installer or build-tool drift, target-built wheel digest changes, hash mismatches, unsupported platforms, incompatible Python, conflicting BioImageFlow versions, forbidden source distributions in offline mode, and changed external attestations.
Pixi fixtures MUST cover channel order and priority, endpoint substitution, credential-independent endpoint identity, virtual-package satisfaction on the login node, and an assumption that remains unverified until orchestrator or worker acknowledgement.
`pylock.toml` fixtures MUST prove that an unrepresentable target build closure fails with `environment-build-lock-incomplete` rather than resolving on the cluster.

### 21.4 Bootstrap and security

Tests MUST cover fresh-root bootstrap, stable-gateway reconnect, setup failure and digest mismatch, shell metacharacters in data, symlink traversal, special files, unsafe permissions, interrupted publication, protocol negotiation, request-size limits, and operation receipts.
Tests MUST also cover leading-option SSH destinations, SFTP operand safety, hard-link and rename races, archive traversal and expansion bombs, unknown gateway digests, corrupt receipts after uncertain scheduler intent, and incremental resource ceilings.
Tests MUST demonstrate that user values cannot become bootstrap shell syntax, that cross-version selection uses a locally trusted artifact digest, and that no operation writes outside approved roots.

### 21.5 Factory validation and planning

Tests MUST cover factory cleanup, bounded secret redaction and its documented transformed-output limit, secret absence from process arguments and receipts, retries rejection, label mismatch, distinct bindings, provider adapters, worker initialization, forbidden Parsl initialization, launch-time drift, recursive portable overrides, planning/runtime routing consistency, and proof that BioImageFlow-controlled validation and planning paths create no scheduler submission, DFK, run, or worker allocation.
They MUST distinguish accepted scheduler syntax, client adapter implementation, installed PSI/J descriptors, validated exact scheduler/provider tuples, and runtime-unverified topology claims.
They MUST also change a Module or Spack target behind unchanged setup bytes and prove that changed required observable facts fail while facts unavailable before allocation remain labeled unverified.

### 21.6 Submission and reconnection

Tests MUST cover successful planned submission, plan-digest stability and sensitivity to the cluster target and stable IDs, lost acknowledgements before and after durable allocation, equal resubmission returning one run, cancellation of a sealed pre-submission plan followed by safe lease release and non-resumption, uncertain PSI/J acceptance without resubmission, attachment without project files, gateway negotiation, cancellation in every state, retry without original paths, and equivalent attached/reconnected parallel-node diagnostics.

### 21.7 Results and cleanup

Tests MUST cover verified download, interrupted retry, corruption, destination conflicts, idempotent identical destinations, external path preservation, atomic publication, cleanup planning, reference conflicts, active-run protection, retained attachment, transfer leases, explicit run deletion, namespace confinement, and quota diagnostics.
Cleanup fixtures MUST include replacement races, mount points, unknown descendants, required old gateways, uncertainty-preserving receipts, and recovery of a partially unlinked deletion tombstone.

### 21.8 End-to-end acceptance

The authoritative acceptance suite includes:

1. a fresh Slurm account using a locked uv project;
2. a setup script loading Modules or Spack;
3. a locked Pixi scientific environment;
4. an offline wheelhouse with network access disabled;
5. an administrator-managed Python with attestation drift detection;
6. prepare, confirmation, submit, disconnect, attach, progress, cancellation, diagnostics, retry, download, and cleanup using public APIs only.

Each of Slurm, PBS, and LSF requires scheduler-adapter integration fixtures and at least one real or hermetic acceptance environment before the corresponding exact compatibility tuple may be reported as validated; shipping syntax support alone is only an implemented client capability.

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
