# Remote Cluster GUI Handoff

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

OpenSSH configuration owns the user, port, keys, agent, `ProxyJump`, and host-key policy.
Do not put credentials, private-key contents, literal secret values, arbitrary SSH options, scheduler directives, shell fragments, or bootstrap commands in a cluster profile.
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
