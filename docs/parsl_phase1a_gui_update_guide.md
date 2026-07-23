# Parsl GUI Update Guide

This guide identifies the final BioImageFlow surfaces a GUI should integrate after the attached Parsl engine implementation.
It stays at the application boundary; the complete runtime contract is documented in `docs/source/reference/parsl.rst` and the storage contract in `docs/source/reference/output_cache_storage.md`.

## Impact Summary

The GUI must treat execution engines as runtime objects rather than reconstructing execution from a serialized engine name alone.
A workflow may store `engine="parsl"` as a preference, but a Parsl run requires an explicitly constructed `ParslEngine` containing runtime configuration and executor bindings.

The cache is an immutable-record store with guarded current selection.
Run history, latest-node state, optional human-facing outputs, mutable attempts, transient results, and backend diagnostics have distinct paths and lifecycles.
A GUI that reads or constructs storage paths should review all of those integrations.

## Engine Construction and Execution

The GUI may construct the engine directly or call `Workflow.create_engine()` on a workflow whose engine preference is `"parsl"`.
Both paths require:

- exactly one of a live `parsl.Config` or caller-owned DataFlowKernel;
- an `ExecutorBinding` for every selectable executor label;
- optional scoped-node and environment-identity routes;
- optional `ParslTaskPolicy`, shared runtime root, and fixed execution policy;
- an explicit resource ownership choice when resources outlive one execution.

The core attached pattern is:

```python
from bioimageflow import ParslEngine

with ParslEngine(
    parsl_config=config,
    executor_bindings=bindings,
    resource_lifetime="engine",
) as engine:
    result = workflow.compute(engine=engine)
```

Use `resource_lifetime="execution"` for one owned DataFlowKernel per call, `"engine"` to retain an owned DataFlowKernel until `engine.close()`, and `"external"` only with an injected caller-owned DataFlowKernel.
An engine rejects overlapping executions and cannot execute after close.
Independent engine instances may share an external DataFlowKernel; each instance owns and drains only the futures it submitted.

Live Config, DataFlowKernel, executor, provider, credential, and secret objects do not belong in workflow JSON.
The immutable Parsl policy, capacity, attestation, and binding values support `to_dict()` and `from_dict()` when the application needs a separate configuration store.

`compute()` returns final DataFrames.
`compute_steps()` remains available for stepped presentation and must be exhausted or explicitly closed.
Passing an explicit engine overrides the workflow's stored engine preference.

## Progress, Cancellation, and Errors

Pass `on_progress` when constructing the workflow.
`ProgressEvent.status` uses `started`, `row_complete`, `completed`, `cached`, `failed`, and `cancelled`.
Events also expose scoped `node_name`, aligned zero-based row progress, timestamp, message or current/maximum values when available, and opaque `result_key` and `record_id` identities.
Callbacks are serialized, but a GUI must still marshal callback work onto its UI thread.
Events from independent nodes may interleave.

Run `compute()` or consume `compute_steps()` outside the UI thread.
Call `workflow.cancel()` from the UI thread to request cancellation of the active execution.
Cancellation stops new submission, requests cancellation of outstanding work, drains possible writers, preserves records already selected by completed nodes, and raises `WorkflowCancelledError`.
Calling `cancel()` while idle does not affect a later execution.

Remote processing failures raise `ParslTaskError`.
The GUI may display its scoped node, executor label, task and invocation IDs, optional cache-attempt ID, retry value, row position, original exception type and message, and remote traceback.
Configuration, routing, shared-path, origin, capacity, and preflight failures occur before processing submission when possible; present their messages as run-start failures.

## Run State

Each public execution has one `run_<uuid>` identifier and writes:

```text
views/runs/<run-id>/run.json
```

`run.json` contains the run ID, workflow identity, storage path, start and completion timestamps, effective engine such as `parsl:parallel`, BioImageFlow version, target nodes, and `running`, `succeeded`, `failed`, or `cancelled` status.

Successful reusable node results appear at:

```text
views/runs/<run-id>/nodes/<scoped-node>/result.json
```

The result view contains the opaque result and record IDs, cache-hit flag, canonical record reference, and declared output entries.
The latest successful run pointer is `views/runs/latest-success.bioimageflow-link.json`.
Latest state is also maintained independently per node under `views/latest/<scoped-node>.bioimageflow-link.json`, so nodes in the latest view may come from different workflow runs.

Use the portable `*.bioimageflow-link.json` files or public `Storage` helpers to resolve links.
Do not assume symlink support and do not select cache entries by scanning record directories, timestamps, or filenames.

## Cache, Paths, Outputs, and Diagnostics

Reusable results live beneath:

```text
cache/v1/results/<shard>/<result-key>/
```

`current.json` is the only selected record for a result key.
`records/<record-id>/` is immutable and contains `manifest.json`, canonical dataframe transport, and declared assets.
Treat result keys and record IDs as opaque.
Mutable `attempts/<attempt-id>/` data is diagnostic execution state and is never a cache hit or user result.

Returned cache-hit asset paths are absolute paths beneath the selected immutable record.
Persisted manifest and dataframe asset paths are record-relative.
Non-reusable processing returns absolute owned paths beneath `cache/v1/transient/runs/<run-id>/.../assets/`; those invocations have no result key, record, current pointer, or run-node record pointer.
Copy or export transient assets before explicit transient cleanup when the GUI needs longer retention.

Directory outputs are first-class assets.
Read each manifest output's `asset_type`, which is `"file"` or `"directory"`, rather than inferring type from a suffix.
Do not expose paths under attempt `work/`, row scratch, batch scratch, worker-local storage, or record candidate directories.

Optional human-facing projections live under:

```text
outputs/runs/<run-id>/nodes/<scoped-node>/outputs/
outputs/latest/<scoped-node>/
```

The application selects `none`, `pointer`, `symlink`, `copy`, or `hardlink` through the public output-view API.
Probe a mode with `Storage.probe_output_view_mode()` before offering it.
Automatic projection failure is a warning and does not make a successful computation fail; an explicit `Workflow.export_outputs()` call is strict.

Parsl task diagnostics are separate from cache identity:

```text
diagnostics/v1/runs/<run-id>/nodes/<scoped-node>/<invocation-id>/tasks/<task-id>.json
```

Use them for an optional task-details or troubleshooting view.
They include backend, executor, dispatch mode, row positions, tool origin, correlation IDs, status, timestamps, and terminal error type.
A task becomes terminal only after BioImageFlow observes or drains its future.

## GUI Areas to Review

- Engine selection and runtime configuration forms.
- Executor binding, route, capacity, and shared-root configuration storage.
- Session ownership and application-shutdown cleanup.
- Background compute and stepped-iterator lifecycle.
- Cancel-button behavior and terminal run-state rendering.
- Progress aggregation for interleaved scoped nodes and cached nodes.
- Structured Parsl error and remote-traceback presentation.
- Run browser and latest-success/latest-node pointer resolution.
- Cache inspection, invalidation, and corrupt-state reporting.
- File and directory asset rendering from manifest entries.
- Output projection capability probing and export controls.
- Transient-result retention messaging.
- Optional backend-task diagnostics.

## Verification Checklist

- A workflow with stored `engine="parsl"` receives a runtime `ParslEngine` before execution.
- Config-owned, engine-retained, and external-DFK sessions close according to their selected ownership.
- The UI remains responsive during `compute()` and while consuming `compute_steps()`.
- Cancellation ends in a `cancelled` run without exposing partial records.
- Cached, completed, failed, and cancelled nodes render from public events and run metadata.
- The run browser resolves portable pointers and never chooses records by directory scanning.
- File and directory outputs open from selected record or output-projection paths.
- Transient outputs are identified separately from reusable records.
- Missing or corrupt current pointers are reported rather than repaired silently.
- Task diagnostics and remote tracebacks are available without being treated as cache content.
