# BioImageFlow Execution UI Integration

This guide describes the public APIs for an execution UI that supports attached Parsl, submitted-local execution, and managed remote clusters.
A GUI should call the public validation, planning, lifecycle, and run-handle operations instead of reproducing routing logic, inspecting launcher storage, parsing logs for failures, or invoking gateway commands itself.

## Capability discovery

Capability discovery is safe in an ordinary local installation and does not import Parsl, PSI/J, uv, Pixi, scheduler plugins, or SSH libraries eagerly:

```python
from bioimageflow import get_execution_capabilities

capabilities = get_execution_capabilities()
payload = capabilities.to_dict()
```

The report distinguishes laptop client availability from cluster validation.
Only `ClusterValidationReport` can say that a requested scheduler, PSI/J plugin, Parsl executor, and provider-adapter combination was validated for one exact deployment.
Queue availability, nested submission policy, compute-node storage visibility, worker networking, and hardware remain unverified until an operation can observe them.

## Portable node resources

Resource overrides belong to one `ProcessingTool` node and survive recursive graph and archive round trips:

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
Placement values do not change result or cache keys, so an explicit recompute is required to rerun an already cached node with different placement.

## Attached and submitted-local Parsl

`validate_parsl_config_ref()` remains the advanced trusted-factory boundary for attached and submitted-local configurations:

```python
from bioimageflow import validate_parsl_config_ref

validation = validate_parsl_config_ref(
    profile.parsl_config,
    executor_bindings=profile.executor_bindings,
    trusted_factories=administrator_factory_allowlist,
)
```

It resolves the factory in an isolated child process, verifies `Config.retries == 0`, compares executor labels with bindings, and returns sanitized diagnostics without starting a DataFlowKernel, provider allocation, workflow run, or scheduler job.
`plan_distributed_execution()` provides the corresponding non-allocating cache, resource, compatibility, and route preview.

Submitted-local execution uses `submit_workflow(..., launch=OrchestratorLaunchConfig(backend="local"))` and returns a local `WorkflowRun`.
The managed remote path described below uses `RemoteCluster`; it does not accept the submitted-local launch or transport values.

## Store a managed cluster profile

A named GUI profile should serialize `RemoteCluster.to_dict()`.
A fully configured profile contains:

- an OpenSSH host or alias;
- one absolute dedicated cluster root;
- a locked `ClusterEnvironment` or an explicitly external existing Python;
- a `ParslConfiguration` source and JSON-safe arguments;
- a `SchedulerJob` for the orchestrator allocation;
- an optional `SetupScript`; and
- an optional results root.

```python
from datetime import timedelta

from bioimageflow.cluster import (
    ClusterEnvironment,
    ParslConfiguration,
    RemoteCluster,
    SchedulerJob,
    SetupScript,
)

cluster = RemoteCluster(
    host="my-hpc",
    root="/cluster/project/alice/bioimageflow",
    setup=SetupScript.from_file("cluster/setup.sh"),
    environment=ClusterEnvironment.from_uv_project("."),
    parsl=ParslConfiguration.from_file(
        "cluster/parsl.py",
        kwargs={"account": "BIOIMAGE"},
        secret_refs={"registry_token": "REGISTRY_TOKEN"},
    ),
    orchestrator=SchedulerJob(
        scheduler="slurm",
        queue="compute",
        project="BIOIMAGE",
        walltime=timedelta(hours=4),
        cpu=4,
    ),
)
```

OpenSSH configuration owns users, ports, keys, agents, jump hosts, and host-key policy.
Do not store private-key contents, passwords, literal secret values, arbitrary SSH options, shell scheduler directives, or host-key bypass values.

`SetupScript` is trusted non-interactive Bash selected explicitly by the user or administrator.
Its verified copy runs before bootstrap discovery, deployment, validation, the orchestrator, and every managed worker; it is not a package installation hook.
A cluster-resident script requires an expected SHA-256 digest.

`ParslConfiguration.from_file()` snapshots one source file plus only explicitly listed includes.
The factory receives a live `ParslFactoryRuntime`, applies `runtime.worker_init` to every managed provider, and returns exactly `ParslFactoryResult` with one binding per executor label.

## Input controls

For every path-shaped value, present an explicit source choice:

- **Upload from this computer** creates `LocalUpload(Path(...))` only after a user selects a file or directory.
- **Already on the cluster** creates an ordinary normalized absolute `Path`.
- An unresolved relative path blocks remote confirmation.
- A string remains a string even if it looks like a path.

Use `inspect_remote_node_paths(workflow)` to discover unconnected path-shaped node constants and defaults recursively.
Do not special-case a particular tool or mutate private nodes.

Invocation-only overrides use scoped paths consistent with planning and diagnostics:

```python
node_input_overrides = {
    "files": {
        "path": LocalUpload(selected_directory),
    },
    "preprocessing/masks": {
        "files": [LocalUpload(path) for path in selected_masks],
    },
}
```

`LocalUpload` is never stored in a reusable workflow definition.
Preparation assigns collision-safe names, preserves list ordering, and freezes every explicitly authorized laptop byte.

## Prepare, validate, plan, and confirm

Use the explicit lifecycle for a confirmation UI:

```python
connection = cluster.check_connection()
deployment = cluster.deploy(progress=show_deployment_progress)
prepared = cluster.prepare(
    workflow,
    inputs={"images": LocalUpload(selected_directory)},
    node_input_overrides=node_input_overrides,
    lifetime=900,
)
validation = cluster.validate(deployment=deployment)
plan = cluster.plan(
    prepared,
    deployment=deployment,
    validation=validation,
    lifetime=900,
)

show_confirmation(
    connection=connection,
    deployment=deployment,
    manifest=prepared.manifest,
    validation=validation,
    plan=plan,
)

if validation.valid and user_confirmed():
    run = plan.submit()
    save_run_id(run.id)
```

The confirmation should show:

- host and cluster root;
- environment kind, ownership boundary, deployment ID, and whether it was reused;
- setup, environment, project, Parsl, workflow, and upload source sizes and digests;
- scheduler, queue, project, walltime, CPUs, memory, GPUs, and hard-cancellation grace;
- validation evidence, declarations, unverified runtime facts, and diagnostics; and
- the invocation digest, plan digest, preallocated run ID, node cache statuses, effective resources, compatible executors, and selected routes.

Do not display or persist resolved secret values or original private source paths omitted by public manifests.

`prepare()` is entirely local.
`validate()` and `plan()` do not submit a scheduler job, create a workflow run, or allocate a worker.
`deploy()` writes remote state only when the content-addressed deployment is absent.

`PreparedClusterInvocation` and `RemoteExecutionPlan` are process-local owners with `close()` and context-manager support.
Keep them alive through confirmation, and close them when abandoned.
Their strict serialized forms are detached summaries suitable for display and persistence, not substitutes for omitted upload bytes.

One plan represents one logical submission attempt.
Repeated `plan.submit()` after durable acceptance returns the same run rather than creating a duplicate.
If acknowledgement is uncertain, preserve the attempt and preallocated `plan.run_id`; do not create a new plan.

## Direct submission

When a separate confirmation boundary is unnecessary, use the convenient composition:

```python
run = cluster.submit(
    workflow,
    inputs=inputs,
    node_input_overrides=node_input_overrides,
    progress=show_deployment_progress,
)
save_run_id(run.id)
```

The call snapshots all local deployment and invocation sources before its first network mutation, creates or reuses the deployment, validates, plans, transfers, submits one orchestrator job through PSI/J, and returns after the run ID is durable.
Parsl providers may request worker allocations after the orchestrator begins.

## Reconnect and present the run

Persist the cluster host, cluster root, run ID, and last consumed progress sequence.
A later process does not need the original environment project, setup script, Parsl file, workflow, or laptop uploads:

```python
from bioimageflow.cluster import RemoteCluster

cluster = RemoteCluster(host=saved_host, root=saved_root)
run = cluster.attach(saved_run_id)
```

Render the authoritative states `prepared`, `starting`, `running`, `cancel_requested`, `finalizing`, `succeeded`, `failed`, `cancelled`, and `lost`.
Connection loss is an unknown observation, not a failed run.

Consume `run.progress(after_sequence=...)` and render `run.diagnostics()` as independent structured node failures.
Do not parse logs or exception strings to identify failed nodes.

Offer cancellation only when `run.can_cancel` is true:

```python
if cancel_clicked() and run.can_cancel:
    run.cancel()
```

Cancellation is idempotent, first requests cooperative cleanup, and uses scheduler cancellation only according to the configured grace policy.

Offer result download only when `run.result_available` is true:

```python
if download_clicked() and run.result_available:
    result = run.download_result(selected_destination)
```

Download verifies the portable bundle and every content digest before publishing atomically.
Interrupted transfers can be retried safely, and unrelated destination content is never silently replaced.

## Retry and recompute confirmation

Terminal managed runs retain the same retry contract as submitted-local runs:

```python
from bioimageflow import RecomputeRequest

retry_plan = run.plan_retry(
    RecomputeRequest(("analysis/segment",), cascade=True)
)
show_retry_confirmation(retry_plan)
retry = run.start_retry(retry_plan)
```

The gateway performs the preview, revision checks, exact current-pointer invalidation, retained invocation cloning, and verified content-addressed upload reuse.
It never rereads laptop paths.
Starting the same exact plan is idempotent; if submission becomes uncertain, attach to its planned child run ID rather than creating another retry.

## Cleanup confirmation

Managed state is never automatically evicted.
Expose cleanup as its own destructive confirmation:

```python
cleanup = cluster.plan_cleanup(**selected_filters)
show_cleanup_candidates(cleanup.candidates)

if user_confirmed():
    report = cluster.apply_cleanup(cleanup)
```

The plan lists exact candidates, sizes, reference reasons, and destructive consequences without mutating state.
Application revalidates every identity and reference revision and skips changed candidates instead of broadening deletion.
Active runs, retained retries, transfer leases, required gateway publications, and receipts needed for safe submission recovery remain protected.

## Lifecycle effects

| Operation | Cluster contact | Remote mutation | Run | Scheduler allocation |
|---|---:|---:|---:|---:|
| `check_connection()` | Yes | No | No | No |
| `deploy()` | Yes | If absent | No | No |
| `prepare()` | No | No | No | No |
| `validate()` | Yes | Temporary bounded state only | No | No |
| `plan()` | Maybe | Temporary only if validation refreshes | No | No |
| `plan.submit()` / `cluster.submit()` | Yes | Yes | Yes | One orchestrator; workers later through Parsl |
| `attach()` / inspection | Yes | No | No new run | No new allocation |
| `cancel()` | Yes | Run state | No new run | May cancel retained jobs |
| `download_result()` | Yes | Bounded transfer state | No | No |
| `plan_cleanup()` | Yes | No | No | No |
| `apply_cleanup()` | Yes | Deletes confirmed state | No | No |

Never label validation or planning as a test job: neither operation allocates a worker, so neither can prove compute-node mounts, worker-to-orchestrator networking, queue availability, nested scheduler submission policy, or worker hardware.
