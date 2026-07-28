# Submitted Parsl GUI Integration

This guide identifies the application surfaces a GUI should use for separate-process Parsl execution.
The complete runtime API is documented in [Parsl execution](../reference/parsl), and the durable path contract is documented in [Output Cache and Storage Contract](../reference/output_cache_storage).

## Integration Summary

Use `submit_workflow()` when a run must continue outside the GUI process and remain reconnectable.
It returns a storage-backed `WorkflowRun`.

The GUI supplies the workflow with its required `storage_path`.
That exact storage root contains launcher control state, canonical run provenance, cache records, output projections, and diagnostics in distinct namespaces.
The workflow graph and archive do not contain the runtime storage path.

```python
from bioimageflow import (
    OrchestratorLaunchConfig,
    ParslConfigRef,
    submit_workflow,
)

run = submit_workflow(
    workflow,
    inputs=resolved_inputs,
    parsl_config=ParslConfigRef(
        "my_app.parsl_config:build_config",
        {"profile": selected_profile},
        secret_refs={"credential": "BIF_CLUSTER_CREDENTIAL"},
    ),
    executor_bindings=bindings,
    node_routes=node_routes,
    environment_routes=environment_routes,
    shared_runtime_root=shared_runtime_root,
    launch=OrchestratorLaunchConfig(backend="local"),
)
```

The configuration factory must be importable in the orchestrator process and return a Parsl Config.
Persist only JSON-safe factory arguments and opaque secret-reference names.
Keep Config, DFK, executor, provider, credential, and callable objects out of GUI project data and workflow JSON.

## Run Persistence and Reconnection

Persist the run ID together with the workflow's explicit storage root.
That pair is sufficient to reconnect:

```python
from bioimageflow import WorkflowRun

run = WorkflowRun.open(storage_path, run_id)
run.refresh()
```

Render these exact launcher states:

- `prepared`
- `starting`
- `running`
- `finalizing`
- `cancel_requested`
- `succeeded`
- `failed`
- `cancelled`
- `lost`

Treat `finalizing` as a non-cancellable success claim, but do not render success until the run reaches `succeeded`; finalization can still fail or become lost.
Treat `lost` separately from `failed`: the orchestrator disappeared or was force-terminated without proof that providers, writers, and cleanup finished normally.

`WorkflowRun.status` is the last loaded state.
Call `refresh()` before rendering a new state or requesting the result.
Polling may be driven by the GUI's normal background task mechanism.

## Progress, Logs, Cancellation, and Errors

Read progress incrementally:

```python
events = run.progress(after_sequence=last_sequence)
for entry in events:
    last_sequence = entry["sequence"]
    if entry["kind"] == "public":
        render_progress(entry["payload"])
```

Every entry has a globally increasing sequence.
Public payloads contain the same node name, status, row counts, messages, timestamps, result key, and record ID exposed by `ProgressEvent`.
Backend entries are operational events and should remain visually separate from workflow progress.

`run.logs()` returns the available orchestrator stdout and stderr text.
Use it for diagnostics rather than primary status detection.

`run.cancel()`:

- directly cancels a run that is still `prepared`;
- requests graceful cancellation for `starting` or `running`;
- is a no-op once `finalizing` or a terminal state has won.

The GUI should display `cancel_requested` until a terminal state arrives.
When `hard_cancel_after` is configured for a local launch, an unresponsive orchestrator may become `lost` after that grace period even when the GUI has reconnected through a new `WorkflowRun` instance.

`run.result()` returns the same single DataFrame or ordered mapping shape as the submitted call.
Handle:

- `WorkflowRunNotReadyError` while the run is non-terminal;
- `WorkflowRunFailedError` with its structured persisted error;
- `WorkflowCancelledError` for cancellation;
- `WorkflowRunLostError` for an indeterminate terminated run;
- `WorkflowRunResultUnavailableError` when an explicitly pruned or corrupted immutable record prevents historical reconstruction.

At submission or reconnection boundaries, surface `LauncherProtocolError` for invalid launcher metadata and `LauncherStateConflictError` for conflicting guarded operations.
`LauncherError` is their shared public base type.

## Storage Areas the GUI Must Distinguish

One submitted run uses the same run ID in two namespaces:

```text
<storage_path>/launcher/v1/runs/<run-id>/
<storage_path>/views/runs/<run-id>/
```

The launcher tree contains mutable status, progress, logs, serialized invocation inputs, the manual command descriptor, structured errors, and the submitted public return.
The canonical view contains portable run and node provenance.

Reusable cache records remain under:

```text
<storage_path>/cache/v1/results/
```

Optional human-facing output projections remain under:

```text
<storage_path>/outputs/
```

The GUI should use `WorkflowRun`, `Storage`, workflow output-view APIs, and portable pointer readers instead of constructing cache, record, return, or output paths.
Do not read a submitted result from `current.json`: `WorkflowRun.result()` resolves the exact immutable records selected by that run.
Do not present launcher input Parquet files or return transport files as cache records.

## Launch Modes

`local` starts a separate orchestrator process and is the direct desktop/server integration.

`manual` persists the complete submission and a shell-free `command.json` descriptor while remaining `prepared`.
A GUI may display or export that descriptor for an external submitter and later reconnect to the same run.

`slurm`, `pbs`, `lsf`, and `oar` currently raise `BackendNotSupportedError` before run allocation.
Do not expose them as working choices unless a launcher adapter is installed in the library.
Parsl providers configured by the factory still allocate worker resources; launcher mode controls only the orchestrator process.

## GUI Verification Checklist

- The selected workflow always has an explicit storage root.
- Submitted runs persist the storage root and run ID for reconnection.
- Runtime Parsl configuration uses an importable factory reference and JSON-safe arguments.
- Secret values never enter project JSON, submission displays, command text, progress, errors, or logs.
- The run list distinguishes launcher status from canonical cache/run provenance.
- Progress polling resumes from the last observed sequence.
- Cancel remains responsive and renders `cancel_requested` until terminal state.
- Failed, cancelled, lost, not-ready, and result-unavailable outcomes have distinct presentation.
- Results load through `WorkflowRun.result()`.
- Cache browsers continue to use immutable records and public `Storage` operations.
- Launcher inputs, return transports, logs, and diagnostics are not presented as cache content.
- Manual mode exposes the structured command without changing the run ID or storage root.
