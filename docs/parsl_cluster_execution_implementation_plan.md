# PSI/J Cluster Execution Implementation Plan

Status: Work Packages 1 and 2 are implemented and Work Package 3 is the next delivery checkpoint.

## 1. Authority, Goal, and Scope

This plan is governed by `docs/parsl_distributed_engine_specs.md`, especially its opening scope and Sections 13, 16, 17, 18, 19, and 20.
The existing workflow, cache, provenance, launcher, and public-return contracts remain authoritative.
The goal is one narrow client-to-cluster path:

1. A laptop packages a strict workflow request and explicit local uploads.
2. OpenSSH and SFTP transfer that package to a cluster login node.
3. A fixed cluster-side BioImageFlow command validates the package.
4. PSI/J on the login node submits exactly one scheduler job for the orchestrator.
5. The orchestrator runs the existing submitted launcher and the existing Phase 1a `ParslEngine`.
6. Parsl providers allocate worker blocks on the same cluster filesystem as the orchestrator.
7. The laptop polls, disconnects, reconnects, cancels, and retrieves the typed public result through the existing Phase 1b lifecycle.

This milestone supports PSI/J executors for Slurm, PBS, and LSF when the corresponding executor is installed and available at the site.
It does not promise an OAR executor or a BioImageFlow native Slurm, PBS, LSF, or OAR adapter.
It adds no scheduler submission inside `ParslEngine.execute()`.
It adds no worker-side staging layer.
It adds no alternate workflow model, cache, output layout, or provenance model.
It adds no storage-path override to remote submission.
The workflow's existing absolute `Workflow.storage_path` is interpreted on the cluster and remains the sole runtime storage authority.
Remote `shared_runtime_root` and launch work-directory values are likewise cluster paths and are normalized only on the cluster.
Development launcher artifacts written before this milestone receive no compatibility reader, migration, alias, or dual-write path.

## 2. Final Architecture and Reuse Map

```text
laptop
  SSHSubmissionTransport
    OpenSSH JSON command channel
    OpenSSH SFTP bulk transfer
        |
        v
cluster login node
  bioimageflow-cluster-agent
    request validation and idempotency
    cluster-local submit_workflow()
    PSI/J JobExecutor
        |
        v
one scheduler job
  bioimageflow.launcher.orchestrator
    Phase 1b claim, state, progress, logs, cancellation, return finalization
    Phase 1a ParslEngine
        |
        v
  Parsl providers and workers on the shared cluster filesystem
```

Phase 1a is reused without a second execution backend.
The orchestrator constructs the existing `ParslEngine` from the persisted `ParslConfigRef`, `ExecutorBinding` values, routes, shared runtime root, and task policy.
The `ParslEngine` continues to acquire its DFK lazily, preflight selected executors, submit `ProcessingTool` work, drain futures, and clean only owned resources.
PSI/J submits the orchestrator only and never submits individual workflow nodes or Parsl workers.
Phase 1b is reused without a remote run-store abstraction.
The cluster-side command calls cluster-local `submit_workflow()` and opens cluster-local `WorkflowRun` handles.
The authoritative state remains `<Workflow.storage_path>/launcher/v1/runs/<run-id>/status.json`.
The canonical run view remains `<Workflow.storage_path>/views/runs/<run-id>/`.
The immutable cache, current pointers, run views, output views, attempts, diagnostics, and launcher return tree keep their current locations beneath the cluster storage root.
Cluster-side validation requires SSH transport staging to resolve outside and disjoint from `Workflow.storage_path`, and staging never becomes a cache, run view, launcher control tree, or source of launcher truth.
The orchestrator and every selected Parsl worker see the same cluster storage root, shared runtime root, and installed upload object paths for the lifetime of the run.
`DataFrameTool` remains an orchestrator-local barrier.
Its merge, transform, cache publication, progress, and result-key behavior stay on the orchestrator compute node.
No `DataFrameTool` task envelope or remote opt-in is introduced.

## 3. Frozen Public API

The final public addition is deliberately small.

```python
@dataclass(frozen=True, slots=True)
class LocalUpload:
    path: Path

@dataclass(frozen=True, slots=True)
class SSHSubmissionTransport:
    host: str
    staging_root: PurePosixPath
    remote_executable: PurePosixPath
    connect_timeout: float = 15.0

@dataclass(frozen=True, slots=True)
class PSIJLaunchConfig:
    executor: Literal["slurm", "pbs", "lsf"]
    walltime: timedelta
    queue: str | None = None
    project: str | None = None
    cpu_cores: int = 1
    work_dir: PurePosixPath | None = None
    hard_cancel_after: float | None = None

def submit_workflow(
    workflow: Workflow,
    *,
    inputs: Mapping[str, Any] | None = None,
    targets: Sequence[str] | None = None,
    parsl_config: ParslConfigRef,
    executor_bindings: Mapping[str, ExecutorBinding],
    node_routes: Mapping[str, str] | None = None,
    environment_routes: Mapping[str, str] | None = None,
    shared_runtime_root: Path | str | None = None,
    task_policy: ParslTaskPolicy | None = None,
    launch: OrchestratorLaunchConfig | PSIJLaunchConfig | None = None,
    transport: SSHSubmissionTransport | None = None,
) -> WorkflowRun | RemoteWorkflowRun: ...

class RemoteWorkflowRun:
    id: str
    status: str

    @classmethod
    def open(
        cls,
        transport: SSHSubmissionTransport,
        storage_path: PurePosixPath | str,
        run_id: str,
    ) -> "RemoteWorkflowRun": ...

    def refresh(self) -> None: ...
    def wait(self, *, timeout: float | None = None, poll_interval: float = 2.0) -> str: ...
    def progress(self, *, after_sequence: int = 0) -> list[dict[str, Any]]: ...
    def logs(self) -> str: ...
    def cancel(self) -> None: ...
    def result(self, *, destination: Path | str) -> Any: ...
```

`OrchestratorLaunchConfig` remains the local/manual configuration and its backend set becomes exactly `local` and `manual`.
`PSIJLaunchConfig` is the only scheduler-launch configuration.
The persisted launch discriminator is `local`, `manual`, or `psij`; the PSI/J executor name lives inside the PSI/J configuration.
Reserved direct scheduler backend aliases are removed rather than retained as compatibility shims.
`transport=None` preserves cluster-local and local-process submission.
`transport` requires `PSIJLaunchConfig` in this milestone and returns `RemoteWorkflowRun`.
`PSIJLaunchConfig` can also be used by a caller already running on the cluster and then returns the existing local `WorkflowRun`.
`RemoteWorkflowRun.open()` takes the cluster storage path because Phase 1b reconnection is defined by storage root plus run ID; that argument locates an existing run and is not a storage override.
`LocalUpload` is accepted only at a root input position declared path-like by the workflow interface.
Ordinary `Path` values are cluster paths and are serialized without laptop-side resolution, existence checks, upload, or rewriting.
Ordinary strings remain strings even if they look like paths.
Root DataFrames reuse their existing logical-digest and Parquet transport contract, require every typed `Path` cell to be an already absolute cluster path, reject relative paths and `LocalUpload` cells, and leave string cells unchanged.
The laptop validates `ParslConfigRef` syntax, while factory import and opaque secret-reference resolution occur only in the cluster environment.
The example below freezes the intended submit, wait, reconnect, cancel, and result flow.

```python
from pathlib import Path, PurePosixPath
from datetime import timedelta

from bioimageflow import (
    LocalUpload,
    PSIJLaunchConfig,
    ParslConfigRef,
    RemoteWorkflowRun,
    SSHSubmissionTransport,
    submit_workflow,
)

# Construct the workflow with its real cluster storage path.
workflow = build_workflow(storage_path=Path("/cluster/project/bioimageflow"))

# OpenSSH resolves this host alias, user, keys, agent, ProxyJump, and host-key policy.
transport = SSHSubmissionTransport(
    host="my-hpc",
    staging_root=PurePosixPath("/cluster/home/alice/.cache/bioimageflow/transport"),
    remote_executable=PurePosixPath("/cluster/home/alice/venvs/bif/bin/bioimageflow-cluster-agent"),
)

# Only LocalUpload reads laptop bytes; atlas and note are never heuristically rewritten.
run = submit_workflow(
    workflow,
    inputs={
        "images": LocalUpload(Path("./images")),
        "atlas": Path("/cluster/reference/atlas.tif"),
        "note": "leave /this/string unchanged",
    },
    parsl_config=ParslConfigRef(
        "my_project.parsl_config:build",
        {"site": "production"},
    ),
    executor_bindings=bindings,
    launch=PSIJLaunchConfig(
        executor="slurm",
        queue="cpu",
        project="BIOIMAGE",
        walltime=timedelta(hours=2),
        cpu_cores=4,
    ),
    transport=transport,
)

# Wait polls durable Phase 1b state and may be interrupted without affecting the job.
terminal_state = run.wait(timeout=None, poll_interval=5.0)

# Persist run.id and the cluster storage path, then reconnect from a later process.
reconnected = RemoteWorkflowRun.open(
    transport,
    PurePosixPath("/cluster/project/bioimageflow"),
    run.id,
)

# Cancellation is durable and graceful; uncomment while the run is active.
# reconnected.cancel()

# A successful result downloads DataFrames and owned assets into an atomic local directory.
result = reconnected.result(destination=Path("./downloads") / run.id)
```

## 4. Frozen Transport and Scheduler Contracts

The laptop uses the system OpenSSH `ssh` and `sftp` executables through `subprocess` with `shell=False`.
The transport never implements passwords, keyboard-interactive prompts, private-key parsing, agent protocols, or host-key policy.
`host` is an OpenSSH destination or alias with no leading dash, whitespace, control character, or newline.
OpenSSH reads the user's normal configuration, known-host files, agent, keychain, `ProxyJump`, port, and user settings.
All operations set `BatchMode=yes`, retain ordinary strict host-key verification, place `--` before the destination where supported, and fail clearly when authentication needs interaction.
The transport offers no `StrictHostKeyChecking=no`, arbitrary SSH option list, shell prelude, module command, or environment-string escape hatch.
`remote_executable` is an absolute cluster path validated as one safe remote-shell token.
The remote invocation contains only that fixed executable; all variable data travels as bounded JSON on standard input.
Every remote SFTP path is produced by the server or derived from validated request IDs, while explicit local source paths and all remote paths pass through one audited SFTP batch quoting function.
Local subprocess arguments, SFTP batch quoting, and remote JSON encoding receive separate injection and whitespace tests.
The fixed remote command speaks one operation-scoped request and one response per process.
Requests use `bioimageflow.cluster.command.v1` with a UUID request ID, operation, protocol versions, and an exact-key arguments object.
Responses use `bioimageflow.cluster.response.v1` with the same request ID, success flag, exact result or stable structured error, and no traceback unless explicitly requested for diagnostics.
Standard output contains protocol JSON only.
Standard error is diagnostic text and is never parsed as protocol.
Request size, response size, progress page length, log chunk size, file count, per-file bytes, aggregate bytes, nesting depth, and operation duration are bounded.
Unknown schemas, operations, fields, duplicate IDs, malformed paths, invalid UTF-8, excessive payloads, and future versions fail closed.
The command operations are `allocate-upload`, `commit-upload`, `submit`, `inspect`, `read-progress`, `read-logs`, `cancel`, and `prepare-result`.
Each operation uses its own UUID request ID and guarded receipt beneath the staging root; reuse with a different canonical request digest fails; `allocate-upload` returns a separate immutable upload ID used in SFTP paths and `commit-upload`; and `submit` keeps one stable request ID across retries.
Before launcher allocation, the submit receipt durably binds the request ID and digest to one preallocated UUID4 run ID; retries resume the shared internal allocation/dispatch seam with that run ID and cannot allocate another run.
Receipts record request digests, mutation outcome tokens, and the request-to-run mapping, while read responses are regenerated from authoritative sources and receipts never mirror launcher status, progress, logs, errors, or results.
The laptop builds one ready-bundle manifest covering the workflow payload, root DataFrame transports, and every explicit upload; each `LocalUpload` root records its validated NFC basename, root kind, confined POSIX relative paths, file kinds, sizes, and SHA-256 digests.
Files and canonical directory trees wrapped in `LocalUpload` reject symlinks, special files, traversal, duplicate paths, case-colliding paths, and changes observed during packaging.
SFTP writes the server-allocated `.partial/<upload-id>/` tree.
`commit-upload` validates every manifest entry and digest on the cluster, rejects unlisted entries, fsyncs where supported, and atomically renames the tree to `ready/<upload-id>/`.
An incomplete or invalid partial tree is never submitted.
Validated uploads are atomically installed read-only beneath `objects/sha256/<digest>/<root-name>` where the digest covers the root kind, logical root name, and canonical file/tree manifest, preserving observable `Path.name` and `Path.stem`.
The resulting stable cluster object path replaces only the corresponding `LocalUpload` marker before cluster-local invocation serialization.
No run ID or launcher path enters that uploaded-input path identity.
The object area has explicit retention and garbage-collection documentation but is not generalized into a remote run store.
`PSIJLaunchConfig` requires a positive walltime and produces one PSI/J `JobSpec` with one node, one process, the requested cores, `queue` mapped to `JobAttributes.queue_name`, `project` mapped to `JobAttributes.account`, the optional work directory, that exact duration passed to `JobAttributes`, and no native scheduler script fragment or accidental PSI/J ten-minute default.
The executable is the cluster Python used by the installed BioImageFlow environment.
The arguments are the existing shell-free orchestrator module invocation containing only absolute storage root and run ID.
PSI/J sends orchestrator stdout and stderr directly to the existing confined launcher log paths; its separate `JobExecutor` work directory is a fixed shared child of the launcher control directory, reused on attach and retained long enough for PSI/J submit/exit metadata recovery.
PSI/J runs only on the login node and is not imported by the orchestrator job after startup or by Parsl workers.
The launcher writes immutable `psij_intent.json` before calling PSI/J and immutable `psij_job.json` immediately after PSI/J returns the native job ID.
The receipt records schema, run ID, submit token, PSI/J executor name, native job ID, creation time, and safe job metadata.
It contains no credentials, environment dump, command shell text, or scheduler script.
The persisted run mapping and PSI/J submit token make repeated remote submissions idempotent across both allocation and scheduler submission.
Once an intent exists, recovery never calls PSI/J submit again.
If the login process dies after the external submit begins but before receipt installation, retry returns the mapped run still in `prepared` with durable `psij-submission-uncertain` backend metadata and never submits again or invents a terminal outcome.
If the possible job subsequently starts, its normal Phase 1b claim becomes authoritative; otherwise explicit cancellation is the only safe terminal resolution and prevents any late job from executing workflow code.
Receipt-backed monitoring reconstructs the configured PSI/J executor and calls `attach(Job(), native_id)` with the persisted native job ID.
Because PSI/J attach updates status asynchronously, each controller waits only a bounded interval for a non-`NEW` observation; timeout, purged history, or unavailable scheduler detail remains an unknown backend observation and never implies launcher success.
PSI/J-native states and native IDs are emitted only as backend progress metadata.
They do not add public `ProgressEvent.status` values or launcher states.
A scheduler-queued orchestrator remains launcher `prepared` until the orchestrator claims startup.
Cancelling a receipt-backed `prepared` run first commits Phase 1b `cancelled`, then best-effort attaches and cancels the queued PSI/J job.
If the job races to start, the terminal launcher state prevents an execution claim and the job exits without running the workflow.
Cancelling `starting` or `running` commits `cancel_requested`, preserves graceful `Workflow.cancel()`, Parsl future draining, and DFK cleanup, and does not kill the PSI/J job during the grace period.
After `hard_cancel_after`, a later poll or wait may attach and cancel an unresponsive orchestrator job; confirmed termination follows the existing `lost` semantics rather than claiming cleanup.
Scheduler rejection or terminal completion before an orchestrator claim produces a stable failed run with backend details.
Disappearance after startup uses the existing claim-expiry and recovery rules and never reruns workflow code.

## 5. Package Boundaries and Delivery Discipline

`bioimageflow.launcher.types` owns `PSIJLaunchConfig` and its strict JSON codec.
`bioimageflow.launcher.submission` owns common validation, cluster-local allocation, and final launcher dispatch for both direct and transported calls.
`bioimageflow.launcher.backends` dispatches local, manual, or PSI/J launchers but lazily imports the PSI/J adapter.
`bioimageflow.launcher.psij` owns `JobSpec` construction, submit, receipt, attach, observation, and cancellation.
`bioimageflow.launcher.cluster_protocol` owns exact JSON command/response schemas and the cluster command dispatcher.
`bioimageflow.launcher.ssh` owns OpenSSH/SFTP subprocess transport, upload packaging, and request idempotency.
`bioimageflow.launcher.run` remains the cluster-local `WorkflowRun` implementation, while `bioimageflow.launcher.remote_run` owns the transport façade and delegates every state operation to it.
`bioimageflow.launcher.payload` continues to own strict graph/archive payloads and gains no storage field.
`bioimageflow.workflow.loading` continues to require explicit `storage_path`; the cluster command passes the unchanged workflow value from the remote request.
`bioimageflow.parsl.engine` and `bioimageflow.parsl.types` remain the Phase 1a execution boundary and receive no PSI/J or SSH imports.
Storage, cache, workflow, and worker-protocol packages receive no SSH, SFTP, or PSI/J dependencies.
The base `bioimageflow` import remains usable without Parsl or PSI/J.
The bounded `bioimageflow[parsl]` extra remains responsible for Parsl.
A bounded `bioimageflow[psij]` extra installs the pinned PSI/J Python API used by the cluster launcher.
The cluster PSI/J installation must expose the selected Slurm, PBS, or LSF executor and its scheduler commands; missing built-in or site-provided executor descriptors raise an actionable optional-runtime error before submission.
The laptop requires OpenSSH executables but no PSI/J installation.
Each work package uses one dedicated worktree and one primary implementation agent.
Suggested worktrees are `.worktrees/psij-launcher`, `.worktrees/ssh-transport`, `.worktrees/remote-workflow-run`, and `.worktrees/cluster-integration`.
WP1 lands before WP2, WP2 lands before WP3, and WP4 begins integration only after the three focused package gates pass.
An agent starts by inspecting `git status`, records the exact baseline commit, and avoids files owned by another active work package.
Every checkpoint runs focused tests, reviews the diff for secrets and unrelated changes, updates intended documentation, and commits only its own project changes.
Temporary fixtures, request bundles, logs, credentials, and site configuration are never committed.
After merge, a manually created worktree is moved to trash, `git worktree prune` is run, and the merged branch is deleted.

## Work Package 1 — PSI/J Orchestrator Launcher

Objective: replace reserved scheduler aliases with one tested PSI/J launcher that submits and reconnects to one orchestrator job.
Implementation tasks:

1. Add the strict `PSIJLaunchConfig` codec and validate executor, queue, project, walltime, core count, work directory, and cancellation grace values.
2. Change the final launch union to local, manual, or PSI/J and remove direct Slurm/PBS/LSF/OAR backend aliases.
3. Add lazy optional-dependency loading with a clear `bioimageflow[psij]` installation message.
4. Build one PSI/J job specification from the existing `build_orchestrator_argv()` output, fixed single-process resources, and confined launcher log paths.
5. Reject arbitrary native scripts, shell fragments, literal secrets, live PSI/J objects, relative executables, and unsafe working directories.
6. Persist submit intent before the external action and the native job receipt before reporting success.
7. Add request-token idempotency and the fail-closed uncertain-submission rule.
8. Implement receipt validation, bounded post-attach observation past `NEW`, normalized scheduler observations, queued-run reconciliation, and terminal-before-claim failure.
9. Wire only PSI/J-specific queued reconciliation and cancellation hooks through the existing `WorkflowRun.refresh()` and `WorkflowRun.cancel()` paths so cluster-local and transported PSI/J runs behave identically, while delegating graceful active cancellation, claim expiry, and lost-run recovery to the existing Phase 1b control plane.
10. Keep all scheduler observations in backend progress/error metadata and retain the exact Phase 1b launcher state set.

Focused tests:

- Extend `tests/unit/launcher/test_types.py` for strict PSI/J config round trips and malformed values.
- Extend `tests/unit/launcher/test_backends.py` for one submitted job, exact resource and `JobAttributes` mapping, safe argv, lazy imports, logs, and unsupported executor failures.
- Add `tests/unit/launcher/test_psij.py` with fake `JobExecutor`, fake jobs, native receipts, attach after process restart, observation mapping, and cancel races.
- Add injected crashes before submit, after submit, before receipt, after receipt, before claim, and after claim.
- Prove an existing intent is never resubmitted and a repeated request cannot create a second orchestrator job.
- Prove OAR is rejected as an unknown PSI/J executor and is not documented as supported.

Acceptance gate:

- A fake Slurm, PBS, or LSF PSI/J executor receives exactly one single-process orchestrator job.
- A persisted receipt can attach, observe, and cancel through a reopened cluster-local `WorkflowRun` after all live launcher objects are discarded, using the same confined shared PSI/J executor work directory even when its platform default is login-local.
- A queued job remains `prepared`, a started job enters the existing state machine, and an early terminal job becomes a structured failure.
- Active cancellation remains graceful until the configured hard-cancel boundary.
- The ordinary package import and existing local/manual launcher tests pass without PSI/J installed.

Agent checkpoint:

- Commit the launcher types, adapter, schemas, tests, optional dependency metadata, and focused reference changes as one reviewable PSI/J feature.
- Do not begin SSH protocol work in this worktree.

## Work Package 2 — SSH Submission Transport

Objective: submit one idempotent cluster request from a laptop through OpenSSH and SFTP without introducing a remote run store or implicit path rewriting.
Implementation tasks:

1. Add `LocalUpload` and `SSHSubmissionTransport` with strict construction validation.
2. Refactor submission internals just enough to accept a server-preallocated run ID after request binding, then use the same cluster-local allocation, invocation serialization, and launcher dispatch path.
3. Carry the workflow's existing absolute cluster storage path beside, not inside, the strict workflow payload.
4. Preserve `Workflow.from_dict(..., storage_path=...)` as the only materialization seam and reject any request that tries to override the captured cluster storage path.
5. Reuse the existing root-DataFrame logical and Parquet transport codecs after rejecting relative typed path cells and `LocalUpload` cells at the remote boundary.
6. Package only explicit `LocalUpload` file/directory values and reject a marker at a non-path interface position.
7. Preserve cluster `Path` values and arbitrary strings byte-for-value until cluster-side typed decoding.
8. Implement the bounded versioned command protocol and fixed cluster-agent executable.
9. Implement SFTP partial upload, server-side digest validation, content-addressed object installation, atomic ready rename, and idempotent retry receipts.
10. Invoke the cluster-local PSI/J submission path only after a complete upload is committed.
11. Redact secret values from requests, responses, errors, logs, and persisted transport receipts; only existing opaque `secret_refs` cross the boundary.
12. Classify local packaging, OpenSSH connection/authentication, SFTP integrity, remote protocol, remote validation, PSI/J submission, and ambiguous-response failures separately.

OpenSSH behavior tests:

- A fake `ssh` executable captures exact argv, environment policy, JSON stdin, stdout, stderr, timeout, and exit status.
- Tests cover host aliases, `user@host`, configured ProxyJump behavior delegated to OpenSSH, missing agent credentials, unknown host keys, timeouts, dropped connections, and nonzero exits.
- No test or implementation passes passwords, disables host-key verification, invokes a shell locally, or interpolates request values into the remote command.

SFTP and upload tests:

- A fake `sftp` executable exercises spaces, quotes, leading dashes, Unicode, newline rejection, partial transfers, digest mismatches, extra files, symlinks, special files, and directory mutation.
- Crashes before and after request-to-run binding, launcher allocation, PSI/J dispatch, and response emission converge on one ready request, one run ID, and at most one orchestrator job.
- Two concurrent clients with the same submit request ID cannot launch twice.
- Equal canonical uploads with the same logical root name reuse one confined read-only object after manifest revalidation, reject ordinary worker mutation, and distinguish different names, kinds, content, or directory trees.
- No uploaded object path contains a launcher run ID.

Acceptance gate:

- The package-private laptop transport seam can upload a workflow archive, root DataFrame, file, and canonical directory tree and receive one remote run ID.
- An interrupted transfer cannot allocate or launch a workflow.
- A lost submit response can be retried without a second PSI/J job.
- The cluster submission uses the workflow's original cluster storage path and the unchanged Phase 1b control/cache/view layout.
- Unmarked laptop paths are never read and path-looking strings are never rewritten.

Agent checkpoint:

- Commit the SSH client, cluster protocol, command entry point, upload codec, focused tests, and concise protocol documentation.
- Do not expose transported `submit_workflow()` or implement the remote run/result façade in this worktree.

Completion checkpoint:

- Implemented the strict `LocalUpload` and `SSHSubmissionTransport` values, bounded one-shot command protocol, installed cluster-agent entry point, shell-free OpenSSH/SFTP client, canonical upload bundle, durable operation receipts, content-addressed read-only object installation, and package-private remote submission seam.
- Submission binds one preallocated launcher run ID before the existing Phase 1b allocation and PSI/J dispatch path, while preserving the workflow storage, launcher, cache, run-view, and output-view layouts.
- The public transported `submit_workflow()` façade, `RemoteWorkflowRun`, observation, cancellation, log/progress access, and result retrieval remain exclusively in Work Package 3.

## Work Package 3 — Remote WorkflowRun Control and Result Retrieval

Objective: expose the Phase 1b lifecycle remotely while keeping the cluster-local `WorkflowRun` as the only state-machine implementation.
Implementation tasks:

1. Add `RemoteWorkflowRun` as a transport-backed façade with no claim of local `control_dir` or `view_dir` paths, then wire transported `submit_workflow()` to construct it.
2. Make `open()` validate the transport, cluster storage path, run ID, remote submission schema, and current cluster-local launcher schemas.
3. Make `refresh()` call cluster-local `WorkflowRun.refresh()`, which performs the WP1 PSI/J reconciliation when applicable, and return its authoritative reread state to the façade.
4. Make `wait()` poll with monotonic deadlines, bounded intervals, interruptibility, and no server-side process that must survive client disconnection.
5. Make `progress(after_sequence=...)` request bounded pages and preserve global Phase 1b sequence numbers without inventing client sequences.
6. Make `logs()` page base64-encoded raw stdout/stderr chunks by byte offset, detect truncation or replacement, assemble bytes before replacement decoding, and reconstruct the same public combined text.
7. Make transport loss leave the remote run untouched and distinguish an unknown observation from `failed`, `cancelled`, or `lost`.
8. Make `cancel()` invoke cluster-local `WorkflowRun.cancel()`, whose WP1 hook performs the applicable state-specific PSI/J behavior.
9. Make repeated cancel calls idempotent across response loss, queued/start races, finalizing, and terminal states.
10. Add `prepare-result` to validate a succeeded Phase 1b return and build a confined immutable download bundle in transport staging.
11. Copy the existing Parquet frames, self-contained return assets, and exact immutable-record assets named by return locators without consulting `current.json`.
12. Write a download manifest with shape, ordered mapping keys, root output IDs/names, typed path locators, sizes, and SHA-256 digests as its final commit marker.
13. Download into a unique local sibling directory, validate every path and digest, reject symlinks and extras, and atomically install the requested destination.
14. Reuse the existing return loader through a factored asset-root seam so the downloaded value has the same single DataFrame or ordered mapping shape, including fresh laptop-local `SharedArray` rehydration from verified assets.
15. Rewrite only record-owned and return-owned path cells to verified local owned assets.
16. Preserve declared external cluster paths as cluster path values and never guess ownership from string prefixes.
17. Reject an existing destination unless it is the exact previously validated bundle for the same run and manifest.
18. Raise the existing not-ready, failed, cancelled, lost, and result-unavailable errors from remote structured data, with transport/integrity errors remaining distinct.

Focused control tests:

- Extend `tests/unit/launcher/test_submission_api.py` for transport/config validation and the `RemoteWorkflowRun` return, while keeping local `WorkflowRun` tests as the lifecycle parity oracle.
- Add `tests/unit/launcher/test_remote_run.py` for fake-transport refresh, wait, reconnect, cancellation, response loss, progress pagination, log offsets, truncation, protocol limits, and transport errors.
- Verify a new client process reconnects from transport, cluster storage path, and run ID only.
- Verify polling mutations are limited to the existing Phase 1b recovery rules and authorized PSI/J reconciliation or hard-cancel paths.

Focused result tests:

- Reuse the cases in `tests/unit/launcher/test_returns.py` for single, mapping, renamed root output, zero-output, external path, record asset, transient asset, directory asset, record-backed `SharedArray`, and pruned record behavior.
- Inject missing files, extra files, traversal, symlink escapes, frame digest mismatch, asset digest mismatch, stale current pointers, interrupted download, and destination collisions.
- Verify a failed download never exposes a partial destination and a retry can reuse a valid server bundle.
- Verify owned result paths are local after materialization while external cluster paths remain unchanged.

Acceptance gate:

- A laptop can poll progress and logs, exit, reconnect, cancel, and observe the same durable Phase 1b state.
- The cluster workflow continues while every client is disconnected.
- A succeeded typed return and every owned asset materialize atomically on the laptop.
- Historical record addressing never depends on current cache selection.
- No launcher state, canonical cache record, run view, or output view is duplicated into transport staging.

Agent checkpoint:

- Commit the remote façade, paged control operations, download bundle, factored return loader, errors, and focused tests.
- Keep GUI code out of this worktree.

## Work Package 4 — End-to-End Integration, Documentation, and GUI Handoff

Objective: prove the complete laptop-to-login-to-orchestrator-to-Parsl path and document the final library and GUI integration contract.
Integration scenarios:

1. Submit a root-interface workflow with a root DataFrame, one `LocalUpload`, one cluster path, and a path-looking string.
2. Submit an ad hoc single target and ordered multiple targets.
3. Execute a `DataFrameTool` on the orchestrator compute node followed by a Parsl `ProcessingTool`.
4. Execute a Parsl `ProcessingTool` followed by an orchestrator-local `DataFrameTool`.
5. Exercise archive custom sources through the existing shared runtime root and worker preflight.
6. Poll queued scheduler metadata, public progress, backend progress, stdout, stderr, success, and structured remote task failure.
7. Disconnect before scheduler start, during execution, during finalization, and during result download, then reconnect.
8. Cancel while queued, starting, running, and finalizing and verify the existing race outcomes.
9. Materialize single, mapping, zero-output, record-owned, transient-owned, record-backed `SharedArray`, and external-path results.
10. Verify one scheduler orchestrator job can cause Parsl providers to allocate multiple independent worker jobs without PSI/J managing those workers.

Deterministic CI uses fake OpenSSH/SFTP and fake PSI/J executors for every state, retry, cancellation, and crash boundary.
`tests/integration/parsl/test_launcher.py` gains an in-process fake-cluster composition that executes the real cluster command and real local Parsl runtime where practical.
The existing `tests/integration/parsl/test_process_executor.py`, `test_semantics.py`, and `test_thread_executor.py` continue proving Phase 1a semantics.
Optional real-site smoke tests are marker-gated, skipped by default, and configured only through environment names or local untracked files.
One smoke test is provided for each available PSI/J Slurm, PBS, or LSF site, but the release gate requires only the executor types actually available to maintainers.
Each smoke test submits a tiny workflow whose Parsl `ProcessingTool` reads an explicit `LocalUpload`, observes the native receipt, reconnects in a second process, verifies one orchestrator scheduler job, downloads the result, and cleans only its transport staging fixture after terminal completion.
No CI or documentation embeds a hostname, account, queue, credential, home path, or scheduler command from a real site.
Documentation tasks:

- Replace the reserved scheduler-backend text in `docs/source/reference/parsl.rst` with the PSI/J architecture and final API example.
- Update installation documentation for the laptop OpenSSH prerequisite, cluster `bioimageflow[parsl,psij]` environment, and site executor plugin.
- Document the unchanged cluster storage/cache/view/output layout, the separate transport staging root, and the requirement that installed upload objects remain available for every referencing run.
- Document `LocalUpload`, default cluster-path semantics, root DataFrame transport, and the prohibition on heuristic string rewriting.
- Document native receipt/attach behavior, uncertain submission, queued status, graceful cancellation, hard-cancel loss, and reconnect requirements.
- Document progress cursors, log polling, result destination atomicity, external cluster paths, owned local assets, retention, and stable errors.
- Update the Parsl acceptance traceability table with every new focused and integration test.

The final GUI handoff is a concise implementation guide rather than GUI code.
It defines fields for OpenSSH host alias, transport staging root, remote executable, cluster workflow storage path, PSI/J executor, queue, project, walltime, cores, and optional hard-cancel grace.
It tells the GUI to use a file/directory chooser only to create explicit `LocalUpload` values.
It tells the GUI to render ordinary workflow paths as cluster paths and never probe them on the laptop.
It defines status presentation using the existing launcher states with scheduler state and native job ID as secondary backend metadata.
It defines resumable progress/log cursors, disconnect/reconnect UX, queued and active cancellation messages, and the non-cancellable `finalizing` state.
It requires the GUI to persist transport configuration name, cluster storage path, and run ID, never credentials or literal secret values.
It requires an explicit local result destination and distinguishes downloaded owned assets from external cluster paths.
It lists stable transport, protocol, launcher, PSI/J, run, and result-integrity error codes and the user action for each.
Final validation follows the README and runs checks proportionate to the touched packages:

```bash
uv run ruff check .
uv run pyright
uv run python scripts/check_file_sizes.py
uv run python scripts/check_import_boundaries.py
uv run pytest tests/unit/test_package_artifacts.py
uv run pytest tests/unit/launcher -m "not slow"
uv run pytest tests/integration/parsl -m "parsl and not slow"
uv run pytest tests -m "not slow and not acceptance and not packaging and not package_tools and not complete and not wetlands and not public_data and not external_binary and not sairpico_binary and not model_runtime and not parsl"
uv run sphinx-build -W --keep-going docs/source docs/_build/html
```

Release acceptance:

- Exactly one PSI/J orchestrator job is submitted for one accepted remote request.
- Slurm, PBS, and LSF use PSI/J rather than BioImageFlow scheduler command parsing; OAR is not promised.
- The workflow storage path, cache, launcher control, canonical views, output views, and public-return tree remain on the cluster in their existing layout.
- Workflow payloads, explicit uploads, and root DataFrame values cross from laptop to cluster; ordinary paths and typed DataFrame path cells remain cluster paths, and arbitrary strings are preserved.
- Progress, logs, cancellation, errors, reconnect, return shape, and owned-asset materialization work after laptop restart.
- Partial uploads, protocol injection, tampered downloads, missing immutable records, and ambiguous PSI/J submission fail closed.

## 6. Explicitly Deferred Work

S3 or any object-store transport is out of scope.
A generic remote run-store or launcher repository abstraction is out of scope.
Durable URI identity, URI path cells, and platform-wide URI changes are out of scope.
Generic no-shared-filesystem worker staging and `ArtifactStager` are out of scope.
Remote or partitioned `DataFrameTool` execution is out of scope.
Streaming workflow execution or streaming results are out of scope.
Worker package installation, environment creation, and arbitrary remote bootstrap commands are out of scope.
Native scheduler adapters, scheduler script templates, OAR support, and scheduler command parsing are out of scope.
Compatibility readers, development-artifact migration, deprecated aliases, and dual schema support are out of scope.
GUI implementation is out of scope; only the final integration guide is delivered.
