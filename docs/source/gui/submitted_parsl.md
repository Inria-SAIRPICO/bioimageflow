# BioImageFlow Execution UI Integration

This guide describes the public APIs for building a BioImageFlow execution UI.
A GUI application should configure public values, call public validation and planning operations, start or reconnect to executions, and consume callbacks or reconnectable run handles.
It should not reproduce routing logic, inspect launcher storage, parse logs for node errors, or invoke cluster-agent operations directly.

## Capability discovery

Capability discovery is safe in an ordinary local installation and does not import Parsl or PSI/J:

```python
from bioimageflow import get_execution_capabilities

capabilities = get_execution_capabilities()
payload = capabilities.to_dict()
if not capabilities.capabilities["submitted_remote_parsl"].supported:
    disabled_reason = capabilities.capabilities["submitted_remote_parsl"].reason
```

The report covers Direct, Wetlands, attached Parsl, submitted-local Parsl, submitted-remote Parsl, PSI/J launch and pre-launch upload, remote profile validation, portable resource overrides, non-allocating planning, structured node failures, and immutable upload preparation.
Direct, Wetlands, planning, public value validation, remote-profile protocol support, resource overrides, diagnostics, and local preparation are built in.
Attached and submitted-local execution require the `parsl` extra.
Cluster submission requires `parsl`, `psij`, the selected PSI/J executor plugin, and the cluster agent in the remote installation.

## Portable node resources

Resource overrides belong to one `ProcessingTool` node and survive recursive graph and archive round-trips:

```python
from bioimageflow import NodeResourceOverrides

segment_node.set_resource_overrides(
    NodeResourceOverrides(
        cpu=8,
        gpu=1,
        memory="32GB",
        gpu_memory="16GB",
        max_concurrent=2,
    )
)
effective = segment_node.effective_resources
```

Missing fields inherit the tool's `ResourceSpec`.
CPU, GPU, memory, and GPU-memory values cannot be below the declaration.
`max_concurrent` is a cap: it may lower but cannot raise a finite declared cap, and zero means unlimited only when the declaration permits it.
`DataFrameTool` and workflow-boundary nodes do not accept overrides.
Wetlands row dispatch and Parsl worker requirements consume the effective value.
Placement changes do not change result or cache keys, so an explicit recompute is required to rerun an already cached node with new placement.

## Local profile validation

Use `validate_parsl_config_ref()` for attached and submitted-local profile testing:

```python
from bioimageflow import validate_parsl_config_ref

validation = validate_parsl_config_ref(
    profile.parsl_config,
    executor_bindings=profile.executor_bindings,
    trusted_factories=administrator_factory_allowlist,
)
if not validation.valid:
    show_diagnostics(validation.diagnostics)
```

The operation accepts only the existing finite JSON-safe `ParslConfigRef` arguments and environment-variable secret references.
The explicit `trusted_factories` allowlist is mandatory; configuration resolution never defaults to trusting every importable callable.
It resolves the factory in an isolated spawned process, verifies `Config.retries == 0`, compares actual executor labels with bindings, returns structured sanitized diagnostics, and terminates the validation process.
It does not create a DataFlowKernel, workflow run, provider allocation, or scheduler job.
The operation reports the missing environment-variable reference, never its value.

After successful validation, attached execution may use the same trusted boundary:

```python
from bioimageflow import ParslEngine

with ParslEngine.from_config_ref(
    profile.parsl_config,
    executor_bindings=profile.executor_bindings,
    trusted_factories=administrator_factory_allowlist,
    environment_routes=profile.environment_routes,
    task_policy=profile.task_policy,
) as engine:
    result = workflow.compute(engine=engine)
```

Construction resolves the configuration but does not start a DataFlowKernel.
The first uncached processing execution may acquire the DFK and provider workers after the run has been accepted.

## Static distributed planning

Use the public planner for command-time preflight:

```python
from bioimageflow import plan_distributed_execution

plan = plan_distributed_execution(
    workflow,
    targets=requested_nodes,
    executor_bindings=profile.executor_bindings,
    node_routes=run_local_node_routes,
    environment_routes=profile.environment_routes,
    shared_runtime_root=profile.shared_runtime_root,
    storage_mode="shared_fs",
    task_policy=profile.task_policy,
)
```

Each `DistributedNodePlan` contains the scoped path, cache-derived execution status, whether the node will dispatch, normalized effective CPU/GPU/memory/GPU-memory/concurrency requirement, compatible executor labels, selected route and route reason when unambiguous, tool-origin mode, environment name and canonical identity, storage mode, per-executor incompatibility reasons, and structured diagnostics.
The enclosing plan records the validated `ParslTaskPolicy` that will bound row chunking and unfinished futures at runtime.
The planner shares requirement derivation, binding compatibility, and route resolution with runtime startup.
Cached and skipped processing nodes remain visible in the plan but do not require or resolve a worker route because runtime will not dispatch them.
It compiles the requested recursive scope but does not create storage runs, materialize archives, provision Wetlands, import Parsl, start a DFK, probe workers, allocate provider blocks, or submit scheduler jobs.
Runtime executor probing remains a post-acceptance operation before the first processing task.

Plans cross a process boundary with `plan.to_dict()` and `DistributedExecutionPlan.from_dict(payload)`.

## Remote profile validation

Use the public operation rather than invoking a private agent command:

```python
from bioimageflow import validate_remote_execution_profile

validation = validate_remote_execution_profile(
    transport=profile.transport,
    parsl_config=profile.parsl_config,
    executor_bindings=profile.executor_bindings,
    launch=profile.launch,
    storage_path=cluster_workflow_storage,
)
```

The normal SSH command path validates connection and transport configuration.
The cluster operation imports and invokes the factory under the trusted submitted-profile rules, resolves secret environment references on that host, verifies `retries=0`, checks actual executor labels, validates absolute storage and staging paths and PSI/J work-directory semantics, and confirms that the requested PSI/J executor plugin is available.
It creates neither launcher run nor scheduler job and reports `allocation_created=False` and `workflow_run_created=False`.
It cannot prove future worker-node shared-filesystem accessibility; runtime executor preflight performs that check after run acceptance.

## Immutable LocalUpload preparation

Prepare explicit laptop uploads before showing final confirmation:

```python
from bioimageflow import LocalUpload, prepare_remote_submission

prepared = prepare_remote_submission(
    workflow,
    inputs={"images": LocalUpload(selected_path)},
    targets=None,
    parsl_config=profile.parsl_config,
    executor_bindings=profile.executor_bindings,
    environment_routes=profile.environment_routes,
    task_policy=profile.task_policy,
    launch=profile.launch,
    lifetime=900,
)
manifest_payload = prepared.manifest.to_dict()
```

Preparation copies the exact file or directory bytes, workflow graph, invocation, and launcher values into a private owned bundle.
The manifest contains stable entry digests, typed external sources, and an overall bundle digest and contains no resolved secrets.
Changing or deleting the original `LocalUpload` path after preparation cannot change the staged invocation.

## Orchestrator pre-launch scripts

A GUI may attach one explicit `PreLaunchScript` to a PSI/J submission:

```python
from bioimageflow import PreLaunchScript

uploaded = PreLaunchScript.from_local_file(selected_local_script)
pinned_cluster = PreLaunchScript.from_cluster_file(
    "/shared/bioimageflow/site-init.sh",
    expected_digest=known_sha256,
)
unpinned_cluster = PreLaunchScript.from_cluster_file(
    "/shared/bioimageflow/site-init.sh"
)
```

`from_text()` and `from_local_file()` enter the immutable prepared bundle and appear as digest-bound manifest entries.
`from_cluster_file()` appears in `manifest.external_sources` with its cluster path and optional expected digest because local preparation cannot observe its bytes.
Present an unpinned cluster source clearly: confirmation binds its path, while the cluster agent snapshots and records the bytes observed at submission.
With an expected digest, a mismatch fails before launcher allocation and scheduler submission.

Pass the value to both direct and prepared submission paths through `pre_launch=`.
BioImageFlow installs every source as one read-only run-owned artifact and gives PSI/J that path, never the original mutable cluster path.
The script is sourced once on the scheduler job's service node before the orchestrator starts; it is not Parsl worker initialization and its output is not a structured node diagnostic.
The GUI must not present non-allocating profile validation as proof that the future service node can see the path or execute module, Spack, Conda, or other site commands.
Do not accept scripts implicitly from workflow archives, and warn against literal secrets because the script artifacts contain plaintext and scheduler logs may contain anything the script prints.

After confirmation, consume the same object exactly once:

```python
try:
    run = prepared.submit(profile.transport)
finally:
    prepared.close()
```

`submit()` re-verifies the staged manifest, uploads those bytes, binds the content-addressed committed object to submission, never rereads the originals, and closes local staging after success.
Expired, abandoned, or failed preparations are cleaned with `close()` or context-manager exit.
An expired, closed, modified, or already submitted preparation is rejected.
The object is intentionally live and process-local; persist only its serializable manifest and keep ownership of the live object in the preflight-token service.

Use `submit_workflow(..., transport=SSHSubmissionTransport(...), launch=PSIJLaunchConfig(...))` for a workflow launched from a laptop on a Slurm, PBS, or LSF cluster.
The call returns a `RemoteWorkflowRun`.
The launcher submits one PSI/J orchestrator job; the Parsl configuration used by that orchestrator owns all provider and worker allocation.

## Configuration fields

A named GUI cluster profile should contain:

- an OpenSSH host or host alias;
- an absolute transport staging root visible on the cluster login node;
- the absolute cluster path to `bioimageflow-cluster-agent`;
- the workflow's absolute cluster storage path;
- the PSI/J executor name: `slurm`, `pbs`, or `lsf`;
- optional queue and project/account;
- positive walltime and orchestrator CPU core count;
- an optional hard-cancel grace period.
- an optional explicit pre-launch source: inline text, local file, or cluster file with an optional expected digest.

OpenSSH configuration owns the user, port, keys, agent, `ProxyJump`, and host-key policy.
Do not put credentials, private-key contents, literal secret values, arbitrary SSH options, or scheduler directives in a cluster profile.
Represent orchestrator initialization only through the typed `PreLaunchScript` field rather than a generic SSH command or scheduler fragment.
The cluster environment must already contain `bioimageflow[parsl,psij]`, the selected PSI/J executor plugin, the workflow's configuration factory, and the tool environments used by workers.

## Input controls

A laptop file or directory chooser creates a `LocalUpload(Path(...))` only after the user explicitly chooses upload semantics.
Every ordinary `Path` value is a cluster path and must never be probed, resolved, or uploaded by the GUI.
A string stays a string even when it looks like a path.
Root DataFrames cross as verified Parquet values; typed `Path` cells must already be normalized absolute cluster paths, while string cells remain strings.

Show the transport staging root separately from workflow storage.
Transport staging contains submission bundles, explicit upload objects, operation receipts, and prepared result downloads.
Workflow launcher state, cache records, run views, output views, diagnostics, and returns remain under the workflow storage path and are never mirrored into transport staging.

## Run persistence and presentation

Persist only the cluster profile name, workflow storage path, run ID, and the last consumed progress sequence.
Do not persist credentials or resolved secret values.
A later process reconnects with:

```python
run = RemoteWorkflowRun.open(transport, storage_path, run_id)
```

Render the authoritative launcher states `prepared`, `starting`, `running`, `cancel_requested`, `finalizing`, `succeeded`, `failed`, `cancelled`, and `lost`.
Scheduler state and the native job ID are secondary backend metadata from progress events.
A queued scheduler job normally remains launcher `prepared` until the orchestrator claims the run.

Resume public and backend progress from the last global sequence.
`run.logs()` reads bounded stdout/stderr snapshots by byte offset internally, assembles bytes before decoding text, and returns the complete currently available combined text.
Replace the displayed log snapshot after each call rather than persisting an internal byte cursor that the public API does not expose.
Connection loss is an unknown observation, not a failed run, and the cluster run continues without a connected GUI.

Allow cancellation in `prepared`, `starting`, and `running`.
Explain that `prepared` cancellation stops a queued job, active cancellation first requests graceful workflow and Parsl cleanup, and an optional hard cancellation after the grace period becomes `lost`.
Disable cancellation in `finalizing` and terminal states because finalization or a terminal outcome has already won the durable race.

Submitted-local execution uses `submit_workflow(..., launch=OrchestratorLaunchConfig(backend="local"))` and returns `WorkflowRun`.
Remote execution may use `submit_workflow(..., transport=transport, launch=PSIJLaunchConfig(...))` directly when no confirmation token is needed, or the prepared boundary above when uploads must be confirmation-bound.

```python
run = submit_workflow(
    workflow,
    inputs=inputs,
    parsl_config=profile.parsl_config,
    executor_bindings=profile.executor_bindings,
    environment_routes=profile.environment_routes,
    task_policy=profile.task_policy,
    launch=profile.launch,
)

same_run = WorkflowRun.open(workflow.storage_path, run.id)
same_run.refresh()
events = same_run.progress(after_sequence=last_sequence)
same_run.cancel()
```

Remote reconnection and cancellation use:

```python
run = RemoteWorkflowRun.open(transport, storage_path, run_id)
run.refresh()
events = run.progress(after_sequence=last_sequence)
run.cancel()
```

Persist only the transport profile reference, cluster storage path, run ID, and last consumed sequence.

## Structured node failures

Attached execution emits `ProgressEvent(status="failed", diagnostic=NodeFailureDiagnostic(...))`.
Submitted execution persists the same diagnostic as an independent `kind="diagnostic"` progress event, and reconnectable handles decode it:

```python
for diagnostic in run.diagnostics():
    render_node_failure(
        path=diagnostic.scoped_node_path,
        category=diagnostic.category,
        exception_type=diagnostic.exception_type,
        message=diagnostic.message,
        traceback=diagnostic.traceback,
        attempt_id=diagnostic.attempt_id,
        terminal=diagnostic.terminal,
        retry_status=diagnostic.retry_status,
    )
```

Concurrent node failures are separate values keyed by scoped path and are not collapsed into the one primary exception raised by `compute()`.
Messages and tracebacks are sanitized before serialization.
Do not parse exception strings, logs, or task artifact paths.

## Allocation and lifecycle summary

| Public operation | Imports optional runtime | Creates run | DFK/workers | Scheduler job | Required cleanup |
|---|---:|---:|---:|---:|---|
| `get_execution_capabilities()` | No | No | No | No | None |
| `validate_parsl_config_ref()` | Parsl only in child factory process | No | No | No | Automatic child-process cleanup |
| `plan_distributed_execution()` | No | No | No | No | None |
| `validate_remote_execution_profile()` | On remote validation host | No | No | No | One-shot command exits |
| `prepare_remote_submission()` | No | No | No | No | `close()` if not submitted |
| `ParslEngine.from_config_ref()` | Yes | No | Not during construction | No | Close engine after execution |
| `workflow.compute(engine=engine)` | According to engine | Yes | May allocate | Provider-dependent | Engine/resource-lifetime contract |
| `submit_workflow()` or `prepared.submit()` | On execution host | Yes | May allocate after orchestrator starts | PSI/J launch creates one orchestrator job | Reconnect/cancel through run handle |

## Wire-format examples

Capability, validation, plan, diagnostic, and preparation-manifest values are JSON-safe:

```json
{
  "schema": "bioimageflow.parsl_config_validation.v1",
  "valid": true,
  "executor_labels": ["gpu"],
  "retries": 0,
  "diagnostics": []
}
```

```json
{
  "schema": "bioimageflow.node_failure.v1",
  "scoped_node_path": "analysis/segment",
  "category": "execution",
  "exception_type": "RuntimeError",
  "message": "worker failed",
  "traceback": "sanitized traceback",
  "attempt_id": "task-7",
  "retry_status": "terminal",
  "terminal": true
}
```

Consumers must use each public `from_dict()` method instead of accepting unknown keys or guessing future schema versions.

## Stable execution behavior

Direct and Wetlands execution use the same `Workflow.compute()` entry point and progress model.
The attached `diagnostic` field is optional because only failed events carry it, while submitted diagnostics use a dedicated event kind.
Nodes without `resource_overrides` inherit their tool declaration.
Resource placement does not invalidate cache identity.

## Results

Require the user to choose an explicit local destination.
`run.result(destination=...)` downloads into a private sibling directory, verifies the complete immutable bundle, and atomically installs the destination.
A destination may be reused only when it is the exact verified bundle for the same run.
Record-owned and return-owned assets become local paths beneath the destination, including downloaded `SharedArray` backing data.
Declared external cluster paths remain cluster `Path` values and should be labelled as unavailable locally rather than guessed from their spelling.

## Stable error actions

| Category or code | GUI action |
|---|---|
| `ssh-unavailable`, `ssh-connection`, `ssh-timeout`, `ssh-command-failed` | Keep the run identity, show transport unavailable, and offer reconnect or retry. |
| `ssh-authentication`, `ssh-host-key` | Ask the user to repair normal OpenSSH configuration outside the application. |
| `sftp-*`, `unsafe-upload-target`, `unsafe-destination` | Keep partial content hidden and ask for a safe path or retry. |
| `remote-protocol`, `remote-invalid-*` | Stop automatic retries unless the error says the operation is retryable; report a client/cluster installation mismatch or invalid request. |
| `PSIJSubmissionUncertainError` | Retain the prepared run and run ID, warn that submission may have happened, and never offer automatic resubmission. |
| PSI/J executor unavailable or scheduler rejection | Ask the user to select an installed site executor or correct queue, project, walltime, and resource fields. |
| `WorkflowRunNotReadyError` | Continue observation; no result is available yet. |
| `WorkflowRunFailedError` | Show the persisted structured workflow error and logs. |
| `WorkflowCancelledError` | Show normal cancellation. |
| `WorkflowRunLostError` | Explain that backend termination was confirmed without proof of normal cleanup. |
| `WorkflowRunResultUnavailableError`, `result-integrity` | Keep the destination uninstalled and report pruned, missing, corrupt, interrupted, or tampered immutable result data. |

The complete API and operational contract are documented in [Parsl execution](../reference/parsl) and [Output Cache and Storage Contract](../reference/output_cache_storage).
