# Remote cluster experience and public API proposal

## Status

This document proposes a future BioImageFlow public API.
It is an example-led description of the experience we want, not the current API or the final launcher protocol specification.
Names may change after usability and technical review.

## The experience we want

A scientist with SSH access to a cluster should be able to run one laptop-side Python script that refers to their workflow, their Parsl configuration, and their project environment.
BioImageFlow should install or reuse its runtime under that scientist's cluster account, validate the setup, submit the workflow, and return a durable run ID.
The scientist should not have to install `bioimageflow-cluster-agent`, construct transport paths, package the Parsl file as an importable module, or coordinate several cluster directories manually.

The common workflow should be:

```text
describe cluster → build workflow → submit → save run ID → reconnect → download result
```

BioImageFlow still cannot remove facts that genuinely belong to the site.
The user or a reusable site template must identify the SSH host, scheduler, account or project when required, queue, writable cluster root, and any Modules or Spack commands needed by the site.

## Concepts introduced by this proposal

A **remote cluster** is a reusable description of one SSH-accessible execution destination.
It owns remote paths, deployment, validation, submission, and reconnection.
Its `root` is the only operational-storage path a common-path user supplies.
BioImageFlow chooses and records deployment, invocation, run-state, transfer, and result locations beneath that root, so user code never assembles internal remote paths.

A **cluster environment** describes the software that should be available to the BioImageFlow orchestrator and ordinary Parsl workers.
It may come from a locked uv or Pixi project, a standardized Python lock, an offline wheelhouse, a bare project manifest, or an administrator-managed Python installation.

A **setup script** exposes site-provided software before BioImageFlow starts Python.
It is the place for commands such as `module load`, `spack load`, and compiler or CUDA initialization, not for manually installing BioImageFlow.

A **deployment** is an immutable, versioned cluster installation derived from the environment, setup script, BioImageFlow version, Parsl configuration source, and relevant platform information.
Equivalent submissions reuse an existing deployment.

A **scheduler job** describes the small job that runs the BioImageFlow orchestrator.
The Parsl configuration separately describes the worker jobs that perform image processing.

## Cluster prerequisites

The proposed automation assumes:

- the laptop can authenticate using ordinary OpenSSH configuration;
- the user may execute non-interactive commands on the login node;
- the user has one writable cluster directory visible to the orchestrator and workers;
- the user may submit jobs to the selected scheduler;
- the cluster provides a compatible Python interpreter directly or through the setup script;
- worker nodes can reach the shared workflow storage;
- package indexes are reachable from the login node when a selected environment is not fully offline;
- required drivers and scheduler software are already managed by the cluster.

BioImageFlow can install software under the user's account.
It cannot install scheduler services, GPU drivers, privileged system libraries, or change cluster policy.

## Golden journey: Slurm and a uv project

Assume this laptop project:

```text
cell-study/
├── pyproject.toml
├── uv.lock
├── run_cluster.py
├── workflow.py
└── cluster/
    └── parsl.py
```

`pyproject.toml` declares BioImageFlow and the workflow's tool packages, while `uv.lock` pins the complete Python resolution.

### The workflow

`workflow.py` remains an ordinary workflow module:

```python
from pathlib import Path

from bioimageflow import Workflow
from bioimageflow_common_tools import Files
from my_analysis_tools import MeasureImage


def build_workflow() -> Workflow:
    workflow = Workflow(name="measure-images")

    with workflow:
        images = workflow.input("images", Path)
        files = Files()(path=images, name="files")
        measurements = MeasureImage()(image=files["path"], name="measure")
        workflow.output("measurements", measurements["measurements"])

    return workflow
```

The reusable workflow does not contain a laptop path or a cluster result directory.
Those values belong to an invocation and its remote cluster.

### The Parsl configuration

`cluster/parsl.py` is ordinary programmable Python rather than a BioImageFlow scheduler dialect:

```python
from parsl import Config
from parsl.executors import HighThroughputExecutor
from parsl.providers import SlurmProvider

from bioimageflow.parsl import ParslFactoryResult, WorkerSlot


def build(runtime, *, account: str) -> ParslFactoryResult:
    executor = HighThroughputExecutor(
        label="cpu-workers",
        cores_per_worker=1,
        max_workers_per_node=32,
        provider=SlurmProvider(
            account=account,
            partition="compute",
            nodes_per_block=1,
            cores_per_node=32,
            init_blocks=0,
            min_blocks=0,
            max_blocks=4,
            walltime="02:00:00",
            worker_init=runtime.worker_init,
        ),
    )

    return ParslFactoryResult(
        config=Config(executors=[executor], retries=0),
        executor_bindings={
            "cpu-workers": runtime.executor_binding(
                slot=WorkerSlot(cpu=1, memory="4 GB"),
            )
        },
    )
```

Parsl needs scheduler-specific provider settings because Slurm, PBS, and LSF expose different queues and job options.
BioImageFlow does not hide that genuine cluster configuration behind an incomplete generic format.

The factory returns the live Parsl `Config` and its BioImageFlow executor bindings together.
This prevents a submission script from accidentally describing different executor labels or environments from those created by Parsl.

`runtime.worker_init` is generated by BioImageFlow and activates the exact immutable deployment after applying the cluster setup script.
The factory must pass it to every Parsl worker provider that starts a shell.

`runtime.executor_binding()` creates a managed executor binding for the deployment, including its environment identity, BioImageFlow core requirement, supported deployed tool origins, and shared-storage claim.
The user still declares the resources guaranteed to one concurrent task slot because Parsl cannot infer memory or GPU capacity reliably from arbitrary provider code.

Advanced configurations may describe externally managed worker environments and tool origins explicitly, but the managed-deployment path should not expose dependency hashes or repeated storage declarations.

### The laptop script

`run_cluster.py` is the complete common-path script:

```python
from datetime import timedelta
from pathlib import Path

from bioimageflow import (
    ClusterEnvironment,
    LocalUpload,
    ParslConfiguration,
    RemoteCluster,
    SchedulerJob,
)

from workflow import build_workflow


cluster = RemoteCluster(
    host="my-hpc",
    root="/cluster/project/alice/bioimageflow",
    environment=ClusterEnvironment.from_uv_project("."),
    parsl=ParslConfiguration.from_file(
        "cluster/parsl.py",
        factory="build",
        kwargs={"account": "BIOIMAGE"},
    ),
    orchestrator=SchedulerJob(
        scheduler="slurm",
        queue="compute",
        project="BIOIMAGE",
        walltime=timedelta(hours=2),
        cpu=4,
    ),
)

run = cluster.submit(
    build_workflow(),
    inputs={"images": LocalUpload(Path("images"))},
)

print("Run ID:", run.id)
run.wait()

if run.status == "succeeded":
    run.download_result(Path("results"))
else:
    for diagnostic in run.diagnostics():
        print(diagnostic.node, diagnostic.message)
```

The first submission may take time because it creates a deployment.
Later submissions with the same deployment identity reuse it.
The laptop process may close after it records `run.id`.

### What `submit()` does automatically

For the common path, `cluster.submit()`:

1. snapshots the workflow graph, local inputs, setup source, Parsl source, and local environment manifests;
2. calculates stable content digests before contacting the cluster;
3. connects using OpenSSH and the user's existing SSH policy;
4. creates or reuses an immutable deployment beneath `cluster.root`;
5. installs the required BioImageFlow, Parsl, PSI/J, project, and tool packages for that deployment;
6. installs the exact Parsl configuration source as deployment-owned code;
7. invokes and validates the Parsl factory without starting a Parsl coordinator or workers;
8. validates executor descriptions, retries, shared paths, scheduler support, and required secret references;
9. plans effective resources, environment compatibility, cache use, and executor routes without allocating workers;
10. uploads the already-snapshotted invocation and inputs;
11. submits one orchestrator job through PSI/J;
12. returns a reconnectable run handle after the run identity is durable.

Submission consumes the prepared snapshots rather than rereading mutable local files after confirmation.

## Reconnect, cancel, and download

A later process needs only the connection identity and saved run ID.
It does not need the original workflow, environment project, setup script, or Parsl file:

```python
from bioimageflow import RemoteCluster

cluster = RemoteCluster(
    host="my-hpc",
    root="/cluster/project/alice/bioimageflow",
)
run = cluster.attach(saved_run_id)

print(run.status)
for event in run.progress():
    print(event)

run.cancel()
run.wait()
```

Cancellation is idempotent.
It first requests cooperative cancellation and uses scheduler cancellation only according to the configured grace policy.

A successful run downloads a verified portable result bundle:

```python
run.download_result(Path("results"))
```

Download verifies the bundle and publishes it atomically at the destination.
It does not silently replace unrelated files.

## Setup with Modules or Spack

Some clusters expose Python, CUDA, or native libraries only after shell initialization.
The cluster definition may add a setup script:

```python
from bioimageflow import SetupScript

cluster = RemoteCluster(
    host="my-hpc",
    root="/cluster/project/alice/bioimageflow",
    setup=SetupScript.from_file("cluster/setup.sh"),
    environment=ClusterEnvironment.from_uv_project("."),
    parsl=parsl_configuration,
    orchestrator=orchestrator_job,
)
```

For example, `cluster/setup.sh` may contain:

```bash
source /etc/profile
module load python/3.12
module load cuda/12.4
source /shared/spack/share/spack/setup-env.sh
spack load openslide
```

The setup script is sourced before deployment commands, the one-shot remote command, the orchestrator, and Parsl worker initialization.
BioImageFlow itself activates the deployment environment afterward, so users do not repeat a versioned virtual-environment path.

The script must be non-interactive and safe to source more than once.
It should expose site software, not mutate the BioImageFlow deployment or install changing dependencies.

`SetupScript.from_text()` supports small generated scripts.
`SetupScript.from_cluster_file(path, sha256=...)` supports an administrator-managed file only when its expected content digest is supplied.
Requiring a digest keeps deployment identity and confirmation meaningful.

This proposal replaces the narrower `PreLaunchScript` concept because initialization is needed before bootstrap, remote commands, the orchestrator, and workers, not only immediately before PSI/J launch.

## Pixi project

Pixi is useful when a workflow needs Python packages and native scientific packages from Conda channels.

```python
environment = ClusterEnvironment.from_pixi_project(
    ".",
    environment="workflow",
)
```

The project may use `pixi.toml` or Pixi tables in `pyproject.toml`, together with `pixi.lock`.
The lock file, selected Pixi environment, target platform, manifest bytes, and relevant project sources contribute to deployment identity.

BioImageFlow installs or transfers a compatible Pixi executable as part of bootstrap when required.
It runs a locked, non-interactive installation and does not update `pixi.lock` on the cluster.

Pixi cannot supply kernel drivers, scheduler services, or site policy.
Those remain cluster prerequisites or setup-script responsibilities.

## Offline wheelhouse

Clusters often block outbound internet access.
An offline submission can use a laptop-prepared wheelhouse plus an exact lock:

```python
environment = ClusterEnvironment.from_wheelhouse(
    "wheelhouse/",
    lock="pylock.toml",
)
```

Preparation snapshots the lock and every referenced wheel, verifies hashes, and rejects a missing or ambiguous distribution.
The cluster installation uses only the uploaded bundle and performs no package-index access.

The wheelhouse must contain compatible artifacts for the cluster operating system, architecture, and Python version, including BioImageFlow's remote extras and the workflow project.
BioImageFlow should report platform mismatches before attempting scheduler submission whenever the login node provides enough information.

A directory of unpinned wheels is not an environment definition.
The lock or equivalent hashed manifest is therefore required for this constructor.

## Manifests and locks

A project manifest such as `pyproject.toml` or `pixi.toml` declares direct requirements, often as version ranges, and may also contain build and tool configuration.
A lock such as `uv.lock`, `pixi.lock`, or standardized `pylock.toml` records a complete resolution for a particular set of environments and platforms, normally including exact versions and artifact hashes.

A lock is the stronger deployment input, but it is not a promise that compatible artifacts remain obtainable or that two clusters have equivalent drivers and hardware.
BioImageFlow preserves both the selected manifest and lock because the manifest explains user intent while the lock records the resolved dependency set.
A constructor whose expected lock is absent must fail instead of silently resolving from the manifest.

## Administrator-managed Python

A site may already provide a complete tested environment:

```python
environment = ClusterEnvironment.from_existing_python(
    "/shared/apps/bioimageflow/2026.08/bin/python"
)
```

BioImageFlow does not modify this interpreter or its packages.
Deployment records its path and a validation attestation, then verifies the required BioImageFlow version, optional dependencies, workflow tools, and Parsl worker environment.

If the environment changes in place after validation, BioImageFlow cannot make it immutable.
The API and validation report must label this source as externally managed and weaker than a content-owned deployment.
A site that wants reproducible reuse should expose versioned immutable paths.

## Other Python project formats

### Standard `pylock.toml`

```python
environment = ClusterEnvironment.from_pylock("pylock.toml", project=".")
```

A standardized lock is an exact installation input when its artifacts are available from configured indexes or an accompanying wheelhouse.
The lock contents and local project sources contribute to deployment identity.

### Bare `pyproject.toml`

```python
environment = ClusterEnvironment.from_pyproject("pyproject.toml")
```

A bare `pyproject.toml` is a project manifest, not an exact lock.
It commonly contains version ranges and leaves transitive resolution to the installer.

BioImageFlow may resolve it once while creating a deployment, record the complete result, and reuse that immutable deployment.
Creating a new deployment later may resolve different package versions, so the validation and confirmation views must describe this source as reproducible only after resolution.

The preferred order for the first implementation is locked uv projects, locked Pixi projects, existing Python, offline wheelhouse with a lock, standardized `pylock.toml`, and bare `pyproject.toml`.
Dedicated adapters for Poetry, PDM, Conda-lock, Spack manifests, and container recipes are valid later extensions, not requirements for the first public experience.

## Explicit lifecycle operations

`submit()` is the convenient composition, but every consequential phase should also be public:

```python
prepared = cluster.prepare(
    build_workflow(),
    inputs={"images": LocalUpload(Path("images"))},
)
deployment = cluster.deploy()
report = cluster.validate(deployment=deployment)
plan = cluster.plan(prepared, deployment=deployment)
run = prepared.submit(deployment=deployment)
```

`deploy()` returns an immutable deployment ID and sanitized manifest.
`validate()` checks the actual remote factory, environment, executor labels, paths, and scheduler plugin without submitting a scheduler job.
`plan()` consumes a prepared snapshot and uses the same effective-resource, compatibility, and routing logic as execution without creating workers or a workflow run.
`prepare()` is local and freezes exactly what a later confirmed submission will consume.

### Operation effects

| Operation | Contacts cluster | Installs or writes remote files | Submits scheduler job | Creates workflow run | Allocates Parsl workers |
| --- | ---: | ---: | ---: | ---: | ---: |
| `cluster.check_connection()` | Yes | No | No | No | No |
| `cluster.deploy()` | Yes | Yes, if the deployment is absent | No | No | No |
| `cluster.validate()` | Yes | May ensure the deployment exists | No | No | No |
| `cluster.plan()` | Maybe, if no validated deployment description is available locally | May ensure the deployment exists | No | No | No |
| `cluster.prepare()` | No | No | No | No | No |
| `prepared.submit()` | Yes | Yes | Yes | Yes | Later, according to Parsl |
| `cluster.submit()` | Yes | Yes | Yes | Yes | Later, according to Parsl |
| `cluster.attach()` and run inspection | Yes | No, except bounded transfer caches | No | No | No |
| `run.cancel()` | Yes | Updates run state | May cancel existing jobs | No new run | No new workers |
| `run.download_result()` | Yes | Creates a bounded transfer snapshot | No | No | No |

No validation or planning operation may submit a test scheduler job implicitly.
An optional future worker-allocation test must have a different name and an explicit cost warning.
Submitting the orchestrator job may consume the user's scheduler allocation immediately, and its Parsl providers may consume additional allocation later as worker blocks start.

## GUI prepare, confirm, and submit

A BioImageFlow execution UI should use the same API rather than a separate platform protocol:

```python
prepared = cluster.prepare(
    workflow,
    inputs={"images": LocalUpload(selected_directory)},
)
deployment = cluster.deploy()
validation = cluster.validate(deployment=deployment)
plan = cluster.plan(prepared, deployment=deployment)

show_confirmation(prepared.manifest, deployment, validation, plan)

if validation.valid and user_confirmed():
    run = prepared.submit(deployment=deployment)
    save_run_id(run.id)
```

The confirmation manifest should show human-readable sources, sizes, digests, environment kind, cluster destination, deployment reuse or creation, and requested scheduler settings.
Its serialized form must not contain secret values or mutable live objects.

Preparation owns private copies of all local upload bytes.
Changing or deleting the original workflow, setup, Parsl configuration, project manifest, or input after preparation cannot change the submitted invocation.

If submission fails before the remote service durably acknowledges a run ID, the prepared object must report whether retry is safe.
The UI must not guess by resubmitting an operation with uncertain outcome.

After confirmation, a UI may close and later call `cluster.attach(run_id)` to show snapshots, progress events, independent per-node diagnostics, cancellation state, and result availability.

For example, a later GUI process can reconnect without the original project files and offer cancellation or download according to the reported state:

```python
cluster = RemoteCluster(host=saved_host, root=saved_root)
run = cluster.attach(load_run_id())

show_progress(run.snapshot(), run.progress())

if cancel_clicked() and run.can_cancel:
    run.cancel()

if download_clicked() and run.result_available:
    run.download_result(selected_destination)
```

The GUI chooses when to ask for confirmation, cancellation, and a local download destination.
BioImageFlow owns the frozen submission, durable run identity, safe cancellation request, and verified transfer semantics behind those controls.

## Errors and recovery

Public failures should identify the phase and a stable category, give a sanitized human explanation, and include safe recovery guidance.

Examples include:

- **SSH unavailable:** check the named OpenSSH host outside BioImageFlow, then retry connection or deployment.
- **Python unavailable after setup:** correct the setup script or choose an existing Python environment; no scheduler job was submitted.
- **Locked artifact unavailable:** provide a reachable package index or an offline wheelhouse; no partial deployment becomes current.
- **Parsl factory failed:** fix the uploaded factory source or its declared arguments; sanitized diagnostics identify the factory traceback without secret values.
- **Worker description mismatch:** make the `WorkerPool.executor` labels match the Parsl `Config`; no scheduler job is submitted during validation.
- **Shared storage not visible from workers:** runtime preflight fails the affected route before processing data and preserves its node diagnostic.
- **Scheduler rejection:** retain the prepared invocation and report the scheduler reason so the user can adjust queue, account, or limits.
- **Laptop disconnect after submission:** attach with the durable run ID; the orchestrator continues independently.
- **Uncertain submission acknowledgement:** inspect the idempotency receipt through the run API before attempting another submission.
- **Deployment installation interrupted:** ignore the incomplete temporary deployment and safely rerun `deploy()`.
- **Result transfer interrupted:** rerun `download_result()`; verified publication remains atomic.

Concurrently failing workflow nodes retain separate diagnostics with scoped node paths, attempt identities, sanitized tracebacks, and terminal or retry status.
Users and GUIs must not parse logs to recover structured failures.

## Security and reproducibility boundaries

The SSH host uses OpenSSH configuration and host-key policy.
BioImageFlow does not accept private-key bytes, passwords, or disabled host-key verification as profile values.

The Parsl configuration and setup script are executable code.
The laptop user explicitly trusts local files they select, and a GUI must show their digests before confirmation.
Cluster-resident executable sources require pinned digests.

Factory arguments must be finite JSON-safe values.
Secrets are represented only by references to environment-variable names or another future credential provider and are resolved on the cluster.
Secret values never enter manifests, serialized cluster definitions, diagnostics, tracebacks, logs, or representations.

Managed deployment directories are content-addressed, created atomically, and read-only after publication.
A run binds one exact deployment ID and one exact prepared-submission digest.
Mutable aliases such as `current` must never define the meaning of a confirmed run.

Package locks make dependency resolution reproducible but do not guarantee equivalent hardware, kernel drivers, scheduler behavior, or external cluster files.
Validation reports should distinguish content-owned evidence from external claims.

The remote bootstrap command necessarily runs code under the user's cluster account.
The final technical specification must define command construction, quoting, archive extraction, permissions, symlink handling, size limits, cleanup, and idempotency before implementation.

## User and library responsibilities

The user or reusable site template supplies:

- SSH host and writable cluster root;
- scheduler, queue, account, and limits for the orchestrator;
- a Parsl provider configuration for worker jobs;
- a project environment source;
- site initialization commands when needed;
- explicit local uploads versus paths that already exist on the cluster.

BioImageFlow supplies:

- runtime bootstrap and versioned installation;
- remote directory layout beneath the cluster root;
- immutable source and input preparation;
- factory packaging and trusted invocation;
- generated worker activation and managed-environment attestations;
- remote validation and non-allocating planning;
- scheduler submission through PSI/J;
- durable run identity, progress, diagnostics, cancellation, retry, and result transfer.

## Non-goals

This proposal does not:

- invent a scheduler-neutral replacement for programmable Parsl providers;
- install or administer Slurm, PBS, LSF, GPU drivers, SSH, or shared storage;
- promise that every Python or Conda package can be installed on every cluster;
- silently merge incompatible tool environments into one worker environment;
- make arbitrary setup or Parsl code safe when selected from an untrusted source;
- provide task-level Parsl-internal drill-down beyond existing public run data;
- define the final wire protocol, remote directory schema, or cleanup algorithm;
- require built-in Slurm, PBS, or LSF Parsl templates in the first implementation.

Reusable templates remain valuable documentation and may later become convenience builders.
They are not a substitute for the programmable Parsl configuration escape hatch.

## Open design questions for review

1. Should `runtime.worker_init` be mandatory for every provider-backed executor, and can BioImageFlow validate its presence before allocation without relying on Parsl internals?
2. Is `WorkerSlot` enough for the common configuration, or should a managed executor infer CPU capacity from known Parsl executor fields while leaving memory and GPU values explicit?
3. Should `cluster.plan()` require an explicit deployment to make its possible network and installation effects obvious, or is automatic deployment consistent with the convenient API?
4. Should a bare `pyproject.toml` adapter exist initially, given that it is not a reproducible lock?
5. Should `from_existing_python()` permit an unversioned path only with an explicit acknowledgement of weaker reproducibility?
6. Which bootstrap artifact should be authoritative when the laptop runs an editable or unreleased BioImageFlow checkout rather than a published distribution?
7. How should uv and Pixi project source inclusion be bounded so deployment captures required local packages without uploading unrelated repository data?
8. Should cluster-resident setup scripts always require a digest, or is there a clearly named mutable-site-policy mode worth supporting?
9. Should remote result storage always default beneath `cluster.root/results/<workflow-id>`, and what public option selects a different durable results root?
10. Should deployment garbage collection be an explicit `cluster.deployments().prune(...)` operation only, with no automatic deletion during submission?

## Acceptance criteria for the later normative specification

The later normative specification is ready for implementation only when it defines observable behavior and conformance tests for all of the following:

- On a fresh Slurm account satisfying the prerequisites, the golden uv example installs BioImageFlow without administrator action, validates, plans, submits, reconnects from a new laptop process, and downloads a verified result.
- Repeating an equivalent submission reuses the same complete deployment, while changing any identity-bearing environment, setup, BioImageFlow, or Parsl source creates a different deployment.
- Every `ClusterEnvironment` constructor shown here has a fixture that either produces the promised environment or fails before scheduler submission with a structured explanation, and reports whether its source is locked, resolved-once, or externally managed.
- A selected `SetupScript` is applied consistently to bootstrap, validation, the orchestrator, and worker initialization, and its exact bytes or pinned cluster digest appear in confirmation and deployment identity.
- `prepare()` owns immutable copies of all local sources and inputs, and later modification or deletion of the originals cannot alter submission.
- `validate()` and `plan()` submit no scheduler job, allocate no Parsl worker, and create no workflow run; every other operation matches the documented side-effect table.
- Submission is idempotent across lost acknowledgements, returns a run ID only after durable acceptance, and exposes an explicit recovery state when the outcome cannot yet be determined.
- `attach()` succeeds with only SSH connection identity, cluster root, and run ID, including after the submitting process and original project directory no longer exist.
- Cancellation is idempotent in queued, running, cancelling, and terminal states, and preserves a durable final state even if the laptop disconnects.
- Result download detects corruption, resumes or safely retries interrupted transfers, and publishes atomically without overwriting unrelated destination content.
- Each failure phase returns a stable category, sanitized structured diagnostics, allocation state, and safe next action without requiring log parsing.
- Manifests, representations, events, and diagnostics contain secret references but never secret values, and executable setup or Parsl sources cannot change after confirmation.
- One GUI implementation can drive prepare, confirmation, submission, reconnect, cancellation, and download using only this public Python API and serialized JSON-safe reports.
- The normative document defines deployment identity, bootstrap trust and compatibility, state machines, remote schemas and permissions, cleanup, quotas, environment attestation, and compatibility with the rest of BioImageFlow execution.
