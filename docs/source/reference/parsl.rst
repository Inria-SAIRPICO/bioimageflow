Parsl execution
===============

BioImageFlow can dispatch ``ProcessingTool`` work through an attached Parsl runtime while retaining workflow planning, ``DataFrameTool`` execution, cache publication, output views, progress ordering, and recursive workflow assembly in the orchestrator.

Install the optional runtime with ``pip install "bioimageflow[parsl]"``.
Importing ``bioimageflow``, validating or serializing a workflow, and calling ``plan()`` do not import Parsl.

Public GUI preflight
--------------------

BioImageFlow GUIs should use the cohesive public preflight surface instead of importing private Parsl startup or launcher modules:

- :func:`bioimageflow.get_execution_capabilities` reports optional runtime availability without importing Parsl or PSI/J.
- :func:`bioimageflow.validate_parsl_config_ref` resolves a trusted configuration factory in an isolated process and validates secrets, retries, and executor labels without creating a DFK or run.
- :func:`bioimageflow.plan_distributed_execution` uses runtime requirement, compatibility, and routing logic without importing Parsl, allocating workers, submitting a job, or creating a run.
- :func:`bioimageflow.validate_remote_execution_profile` performs the corresponding non-submitting checks on a remote cluster through the public protocol.
- :func:`bioimageflow.prepare_remote_submission` binds explicit ``LocalUpload`` and uploaded pre-launch values to an immutable, digest-verified, single-use submission and identifies cluster-resident pre-launch sources separately.

The complete integration sequence, allocation table, wire examples, diagnostics contract, and lifecycle rules are in :doc:`/gui/submitted_parsl`.

Attached engine construction
----------------------------

A Parsl execution requires explicit runtime configuration.
Construct :class:`~bioimageflow.ParslEngine` with exactly one of a ``parsl.Config`` or an existing DataFlowKernel, plus an :class:`~bioimageflow.ExecutorBinding` for every selectable executor label.
Persistent profiles may instead use :meth:`bioimageflow.ParslEngine.from_config_ref`, which applies the trusted public validation boundary before constructing the engine.

The following local example attests one thread executor for a tool environment:

.. code-block:: python

   from importlib.metadata import version

   from parsl import Config
   from parsl.executors.threads import ThreadPoolExecutor

   from bioimageflow import (
       ExecutorBinding,
       ExecutorCapabilities,
       ParslEngine,
       WorkerEnvironmentAttestation,
       WorkerSlotCapacity,
   )
   from bioimageflow.cache import compute_env_hash

   environment = MyProcessingTool.environment
   label = "local-threads"
   binding = ExecutorBinding(
       label=label,
       environments=(
           WorkerEnvironmentAttestation(
               name=environment.name,
               dependency_hash=compute_env_hash(environment.dependencies),
               allow_flexible_versions=environment.allow_flexible_versions,
               core_requirement=f"bioimageflow-core=={version('bioimageflow-core')}",
           ),
       ),
       capabilities=ExecutorCapabilities(
           storage_modes=("shared_fs",),
           tool_origin_modes=(
               "installed_module",
               "versioned_module",
               "shared_module",
               "source_file",
               "archive_module",
           ),
           slot=WorkerSlotCapacity(cpu=1),
       ),
   )
   config = Config(
       executors=[ThreadPoolExecutor(label=label, max_threads=4)],
       retries=0,
   )

   with ParslEngine(
       parsl_config=config,
       executor_bindings={label: binding},
       resource_lifetime="engine",
   ) as engine:
       result = workflow.compute(engine=engine)

The constructor validates local values but does not start a DataFlowKernel or submit work.
Execution compiles and validates the reachable workflow before acquiring an owned DataFlowKernel.
A fully cached execution does not acquire one.

An application that already owns a DataFlowKernel passes ``dfk=dfk`` and ``resource_lifetime="external"``.
The engine then drains only its own futures and never cleans the caller's DataFlowKernel or unrelated tasks.

Workflow factory and injection
------------------------------

``Workflow.create_engine()`` forwards Parsl runtime values when the workflow preference is ``engine="parsl"``:

.. code-block:: python

   engine = workflow.create_engine(
       parsl_config=config,
       executor_bindings={label: binding},
       resource_lifetime="execution",
   )
   result = workflow.compute(engine=engine)

Passing an explicit engine to ``compute()`` or ``compute_steps()`` overrides the stored workflow engine preference.
A bare ``Workflow(storage_path="./results", engine="parsl").compute()`` fails with an attached-engine construction error because a workflow file cannot carry live Config, executor, provider, credential, or DataFlowKernel objects.

Bindings, routes, and preflight
-------------------------------

Each executor binding declares:

- the exact Parsl executor label;
- the environment identities already installed on its workers;
- supported storage and tool-origin modes;
- homogeneous CPU, GPU, memory, and GPU-memory capacity for one worker slot.

BioImageFlow never installs packages or starts Wetlands from a Parsl task.
Every reachable ``ProcessingTool`` environment must match an attestation, and every :class:`~bioimageflow_core.ResourceSpec` must fit the selected slot.

Routing uses this deterministic order:

1. ``node_routes`` keyed by the complete scoped node name;
2. ``environment_routes`` keyed by the canonical environment identity;
3. the single compatible binding, when exactly one exists.

Missing, ambiguous, unknown, or incompatible routes fail before processing submission.
Each selected executor label then runs a preflight app that proves executor placement, BioImageFlow worker API and core requirements, shared-root access, sentinel write/read/delete capability, readable dependency paths, and every required tool origin.

Shared storage and tool origins
-------------------------------

``storage_mode="shared_fs"`` is the supported transport.
The workflow storage root, attempt and transient directories, source paths, and optional ``shared_runtime_root`` must resolve identically and be accessible where required by the selected workers.
``storage_mode="staged"`` is rejected because no staging contract is defined.

Installed packages, versioned tool modules, shared project modules, verified source files, and materialized archive modules are supported worker origins.
Archive sources require an absolute ``shared_runtime_root`` and are installed atomically after content verification.

Scheduling and task bounds
--------------------------

``execution="workflow"`` follows the workflow's scheduling policy.
``execution="parallel"`` permits independent processing nodes to overlap.
``execution="sequential"`` permits one node and one unfinished row task at a time.
``DataFrameTool`` and recursive workflow boundaries always execute in the orchestrator.

:class:`~bioimageflow.ParslTaskPolicy` controls row packing and submission pressure:

.. code-block:: python

   from bioimageflow import ParslTaskPolicy

   policy = ParslTaskPolicy(row_chunk_size=8, max_in_flight=64)

``row_chunk_size`` creates consecutive row chunks.
``max_in_flight`` bounds unfinished Parsl futures, and ``ResourceSpec.max_concurrent`` can lower that bound for one processing node.
When a node has :class:`~bioimageflow.NodeResourceOverrides`, Parsl consumes its validated effective resource value rather than the shared tool-class declaration.
Whole-node ``process_batch()`` remains one task and emits no row-complete events.

Parsl Config must use ``retries=0``.
BioImageFlow owns deterministic failure selection and therefore rejects opaque scheduler retries.

Lifecycle, cancellation, and errors
-----------------------------------

The ``resource_lifetime`` values are:

- ``"execution"``: create and clean one DataFlowKernel for each execution;
- ``"engine"``: retain an owned DataFlowKernel across executions and clean it on ``close()``;
- ``"external"``: use an injected caller-owned DataFlowKernel.

``close()`` and context-manager exit are idempotent.
An engine rejects overlapping executions and cannot be reused after close.
Closing a stepped iterator or the engine requests cancellation for submitted engine-owned futures, drains all submitted futures, and only then applies resource cleanup.

Call ``workflow.cancel()`` from another thread to cancel the active execution.
Cancellation stops new submission, requests cancellation of outstanding futures, ignores late results, drains writers, and leaves incomplete attempts unselected.
The active cancellation context is cleared when execution finishes, so a later compute does not inherit the request.

Remote failures raise :class:`~bioimageflow.ParslTaskError` with the node, task, executor, attempt, row-position, retry, exception-type, message, and remote traceback identity.
When several tasks fail, the primary exception is selected by compiled workflow and task position rather than completion timing.

Progress and diagnostics
------------------------

Parsl uses the same public ``ProgressEvent`` statuses as the other engines.
Failed events may carry a :class:`~bioimageflow.NodeFailureDiagnostic`.
Row-complete callbacks are serialized and emitted in aligned row order even when futures complete out of order.
Independent node events may interleave.

Run and node provenance use the effective engine name ``parsl:parallel`` or ``parsl:sequential``.
Backend task diagnostics are stored separately from immutable cache records at:

.. code-block:: text

   diagnostics/v1/runs/<run-id>/nodes/<node-key>/<invocation-id>/tasks/<task-id>.json

A diagnostic records task correlation, executor label, mode, retry, row positions, tool origin, status, timestamps, and terminal error type.
It becomes terminal only after BioImageFlow observes the future.
Attempt and task diagnostics do not contribute to result keys or record IDs.
Submitted handles expose the same failed-node values across execution engines through ``WorkflowRun.diagnostics()`` and ``RemoteWorkflowRun.diagnostics()``.

Submitted workflows
-------------------

Use :func:`~bioimageflow.submit_workflow` when execution must continue in a separate orchestrator process and remain reconnectable after the submitting client exits.
The workflow keeps its explicit ``storage_path`` as the runtime storage root; that path is launcher metadata and is not inserted into the serialized workflow graph or archive.

Submitted mode refers to Parsl configuration through an importable factory instead of serializing live Config, executor, provider, callable, or credential objects.
For example, an application module can expose:

.. code-block:: python

   from parsl import Config
   from parsl.executors.threads import ThreadPoolExecutor

   def build_parsl_config(*, max_threads: int) -> Config:
       return Config(
           executors=[
               ThreadPoolExecutor(
                   label="local-threads",
                   max_threads=max_threads,
               )
           ],
           retries=0,
       )

The client submits the workflow with JSON-safe factory arguments and the same strict executor bindings used by attached execution:

.. code-block:: python

   from bioimageflow import (
       OrchestratorLaunchConfig,
       ParslConfigRef,
       submit_workflow,
   )

   run = submit_workflow(
       workflow,
       inputs={"folder": "/data/images"},
       parsl_config=ParslConfigRef(
           "my_application.parsl_config:build_parsl_config",
           {"max_threads": 4},
       ),
       executor_bindings={"local-threads": binding},
       launch=OrchestratorLaunchConfig(backend="local"),
   )

``kwargs`` accepts finite JSON-safe values.
Literal credentials must not be supplied, and secret-looking field names are rejected.
``secret_refs`` maps factory argument names to opaque environment-variable names that the launch host verifies before starting the orchestrator and resolves inside that process.

The invocation uses exactly one mode.
Omit ``targets`` to call ``workflow.compute(inputs=...)`` through the public root interface.
Alternatively, pass immediate registered node names in ``targets`` and omit ``inputs`` to reproduce the single-target or ordered multi-target return contract of ``workflow.compute(*targets)``.
Root DataFrame inputs are externalized beneath the launcher control directory and verified by both Parquet transport digest and canonical logical DataFrame digest before Parsl acquisition.

The local backend starts ``python -m bioimageflow.launcher.orchestrator`` as a separate process with confined stdout and stderr logs.
The manual backend writes a shell-free ``command.json`` descriptor and leaves the run in ``prepared`` until an external actor executes that command.
The PSI/J backend submits exactly one Slurm, PBS, or LSF scheduler job for that same orchestrator.
Install it in the cluster environment with ``pip install "bioimageflow[parsl,psij]"``.
Direct ``slurm``, ``pbs``, ``lsf``, and ``oar`` backend aliases are not accepted, and OAR is not a supported :class:`~bioimageflow.PSIJLaunchConfig` executor.
Parsl providers remain responsible for allocating worker resources; a launcher backend starts only the orchestrator.

For a caller already running on a cluster login node:

.. code-block:: python

   from datetime import timedelta
   from pathlib import PurePosixPath

   from bioimageflow import PSIJLaunchConfig, submit_workflow

   run = submit_workflow(
       workflow,
       parsl_config=parsl_config,
       executor_bindings=bindings,
       launch=PSIJLaunchConfig(
           executor="slurm",
           walltime=timedelta(hours=2),
           queue="cpu",
           project="BIOIMAGE",
           cpu_cores=4,
           work_dir=PurePosixPath("/cluster/project/orchestrator"),
       ),
   )

``PSIJLaunchConfig`` accepts only strict scheduler identifiers and a normalized absolute POSIX working path that exists as a non-symlink directory when initial submission begins.
It has no native directive, custom attribute, environment, live PSI/J object, or shell-fragment field.
``walltime`` is always explicit so PSI/J's default duration is never selected accidentally.
The queue maps to ``JobAttributes.queue_name`` and the project maps to ``JobAttributes.account``.

Orchestrator pre-launch scripts
-------------------------------

An optional :class:`~bioimageflow.PreLaunchScript` prepares the environment in which the submitted orchestrator starts.
It is submission material rather than scheduler configuration, so pass it beside ``launch``:

.. code-block:: python

   from pathlib import Path

   from bioimageflow import PreLaunchScript, submit_workflow

   run = submit_workflow(
       workflow,
       parsl_config=parsl_config,
       executor_bindings=bindings,
       launch=launch,
       pre_launch=PreLaunchScript.from_text(
           """\
           source /etc/profile.d/modules.sh
           module load python
           """
       ),
   )

Use :meth:`~bioimageflow.PreLaunchScript.from_local_file` when the script is a file on the machine making the submission.
BioImageFlow snapshots that file once before allocation retries and preserves its exact UTF-8 bytes:

.. code-block:: python

   pre_launch = PreLaunchScript.from_local_file(Path("cluster-init.sh"))

For transported execution, :meth:`~bioimageflow.PreLaunchScript.from_cluster_file` names a script already present on the cluster:

.. code-block:: python

   pre_launch = PreLaunchScript.from_cluster_file(
       "/shared/bioimageflow/site-init.sh",
       expected_digest="sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
   )

The expected digest is optional.
When present, the cluster agent rejects different content before launcher-run allocation or scheduler submission.
When absent, confirmation binds only the cluster path; the agent snapshots the currently observed bytes and records their digest in the immutable run metadata.
PSI/J always receives the run-owned copy, never the original cluster path.

Scripts must be non-empty UTF-8 and at most 64 KiB.
NUL bytes, symlinks, special files, concurrent source changes, and digest mismatches are rejected.
The supported PSI/J launcher sources the file once on the scheduler job's service node before starting the orchestrator, so a shebang is ignored, exported variables and directory changes persist, and an unhandled failure may prevent orchestrator startup.
The workflow storage path and run artifact must therefore be visible at the same absolute location on that node.
This script does not initialize Parsl workers; use the selected provider's worker initialization for worker modules, CUDA setup, containers, or environment activation.
Do not place literal credentials in a script: its source is absent from JSON metadata and structured diagnostics, but the prepared and run-owned files necessarily contain plaintext and anything printed by the script belongs to scheduler-controlled logs.

Before PSI/J submission, BioImageFlow persists an immutable submit intent.
After PSI/J returns a native job ID, it immediately persists an immutable correlated receipt.
The receipt is sufficient for a later process to reconstruct the same executor with the same confined shared executor work directory, attach to the native job, observe scheduler state, and cancel it.
If submission may have succeeded but the receipt was not installed, the run remains ``prepared``, raises :class:`~bioimageflow.PSIJSubmissionUncertainError`, records scheduler uncertainty as backend progress, and is never submitted again automatically.

Reconnect and observe a run through :class:`~bioimageflow.WorkflowRun`:

.. code-block:: python

   from bioimageflow import WorkflowRun

   run = WorkflowRun.open(workflow.storage_path, run.id)
   run.refresh()
   events = run.progress(after_sequence=0)
   text = run.logs()

   if run.status == "succeeded":
       result = run.result()

The launcher states are ``prepared``, ``starting``, ``running``, ``finalizing``, ``cancel_requested``, ``succeeded``, ``failed``, ``cancelled``, and ``lost``.
``status.json`` beneath ``launcher/v1/runs/<run-id>/`` is authoritative for reconnection.
The portable canonical view uses the same ID beneath ``views/runs/<run-id>/``.
Mutable launcher state, logs, externalized inputs, and return transports never enter the canonical view or cache identity.
A terminal PSI/J scheduler observation confirms backend absence only after an active launcher lease expires, then invokes the existing recovery rules without rerunning workflow code.

``WorkflowRun.cancel()`` directly cancels an unclaimed prepared run or commits ``cancel_requested`` for an active run.
The orchestrator watches durable status and the optional wake-up marker, calls ``Workflow.cancel()``, drains its Parsl work, and finishes as ``cancelled``.
If a local or PSI/J launch config supplies ``hard_cancel_after``, any reconnected run handle can terminate the exact persisted orchestrator process or scheduler job that remains unresponsive after that grace period; confirmed termination becomes ``lost`` rather than claiming normal cleanup.

Submitted success installs a complete public return before the canonical run and launcher become successful.
The return preserves the single DataFrame or ordered mapping shape, exact root output IDs and names, immutable-record asset locations, declared external paths, and self-contained copies of transient owned assets.
Historical loading addresses exact record IDs and never consults ``current.json``.
If an explicitly pruned or corrupted immutable record is required, :meth:`~bioimageflow.WorkflowRun.result` raises :class:`~bioimageflow.WorkflowRunResultUnavailableError`.

The complete launcher storage and retention contract is documented in :doc:`output_cache_storage`.

Laptop submission transport
---------------------------

The ``bioimageflow-cluster-agent`` one-shot command and :class:`~bioimageflow.SSHSubmissionTransport` provide laptop submission and remote lifecycle control.
Passing ``transport=`` with an explicit :class:`~bioimageflow.PSIJLaunchConfig` returns :class:`~bioimageflow.RemoteWorkflowRun`.
Omitting transport runs the same cluster-local submission path and returns :class:`~bioimageflow.WorkflowRun`.

Install OpenSSH ``ssh`` and ``sftp`` on the laptop.
Install ``bioimageflow[parsl,psij]`` and the selected site PSI/J executor plugin in the cluster environment.
OpenSSH resolves host aliases, users, keys, agents, ports, ``ProxyJump``, and host-key policy from its normal configuration.
BioImageFlow uses ``BatchMode=yes`` and provides no password, private-key, host-key bypass, arbitrary SSH option, or SSH shell-setup field.
Use the typed ``PreLaunchScript`` submission value for orchestrator initialization.

The following complete example distinguishes laptop actions from cluster actions:

.. code-block:: python

   from datetime import timedelta
   from pathlib import Path, PurePosixPath

   import pandas as pd

   from bioimageflow import (
       LocalUpload,
       PreLaunchScript,
       PSIJLaunchConfig,
       ParslConfigRef,
       RemoteWorkflowRun,
       SSHSubmissionTransport,
       submit_workflow,
   )

   # Laptop: build the graph with the storage root that exists on the cluster.
   workflow = build_workflow(
       storage_path=Path("/cluster/project/my-workflow/results")
   )
   transport = SSHSubmissionTransport(
       host="my-hpc",  # An OpenSSH Host alias, not a credential record.
       staging_root=PurePosixPath(
           "/cluster/project/my-workflow/transport"
       ),
       remote_executable=PurePosixPath(
           "/cluster/apps/bioimageflow/bin/bioimageflow-cluster-agent"
       ),
   )

   root_table = pd.DataFrame(
       {
           "atlas": [Path("/cluster/reference/atlas.tif")],
           "label": ["/this/remains/a/string"],
       },
       index=["sample"],
   )
   run = submit_workflow(
       workflow,
       inputs={
           # Laptop: package exactly this selected file or directory.
           "images": LocalUpload(Path("./images")),
           # Cluster: preserve this ordinary Path without probing the laptop.
           "mask": Path("/cluster/reference/mask.tif"),
           # Cluster: transfer verified DataFrame values; typed Paths are absolute.
           "samples": root_table,
           # Both sides: preserve strings without path heuristics.
           "description": "/this/remains/a/string",
       },
       parsl_config=ParslConfigRef(
           "my_project.parsl_config:build",
           {"profile": "production"},
       ),
       executor_bindings=bindings,
       shared_runtime_root=Path(
           "/cluster/project/my-workflow/shared-runtime"
       ),
       launch=PSIJLaunchConfig(
           executor="slurm",
           queue="cpu",
           project="BIOIMAGE",
           walltime=timedelta(hours=2),
           cpu_cores=4,
           hard_cancel_after=300,
       ),
       pre_launch=PreLaunchScript.from_local_file(
           Path("./cluster-init.sh")
       ),
       transport=transport,
   )

   # Cluster: the agent validates and commits the upload, then PSI/J submits
   # exactly one orchestrator job. Parsl providers allocate worker blocks.
   # Laptop: these calls are bounded observations and may stop at any time.
   progress_cursor = 0
   for entry in run.progress(after_sequence=progress_cursor):
       progress_cursor = entry["sequence"]
   print(run.logs())
   terminal = run.wait(poll_interval=5.0)

   # A later laptop process needs only this transport profile, storage path,
   # run ID, and its saved progress/log cursors.
   run = RemoteWorkflowRun.open(
       transport,
       PurePosixPath("/cluster/project/my-workflow/results"),
       run.id,
   )
   if terminal == "succeeded":
       result_destination = Path("./downloads") / run.id
       result_destination.parent.mkdir(parents=True, exist_ok=True)
       result = run.result(
           destination=result_destination
       )

Only explicit :class:`~bioimageflow.LocalUpload` and uploaded :class:`~bioimageflow.PreLaunchScript` values read laptop file content.
``LocalUpload`` remains valid only for a path-like root workflow input.
An ordinary ``Path`` is a cluster path.
A string remains a string even when it resembles a path.
Root DataFrames use verified Parquet and logical digests; every typed ``Path`` cell must be a normalized absolute cluster path, and string cells are unchanged.

The transport staging root must be absolute, visible to the login node, and disjoint from workflow storage.
It contains partial and ready submission bundles, content-addressed read-only upload objects, operation receipts, and immutable prepared result downloads.
It never contains a launcher control tree, cache, canonical run view, output view, or authoritative status.
Installed upload objects must remain readable by the orchestrator and every worker for the lifetime of each referencing run.
Operators may remove abandoned partial uploads, expired result bundles, and upload objects that are no longer referenced by any retained run.

The unchanged workflow storage layout remains authoritative:

.. code-block:: text

   <storage>/launcher/v1/runs/<run-id>/   mutable control, logs, return
   <storage>/cache/v1/results/            immutable cache records
   <storage>/views/runs/<run-id>/         canonical run view
   <storage>/outputs/                     optional output projections

PSI/J writes a submit intent before the external scheduler action and a native receipt immediately after the scheduler returns an ID.
Reconnect uses that receipt to attach by native ID.
A queued scheduler job remains launcher ``prepared`` until the orchestrator claims it.
If submission may have happened without a durable receipt, :class:`~bioimageflow.PSIJSubmissionUncertainError` leaves the run prepared and prevents automatic resubmission.

``cancel()`` commits prepared cancellation before best-effort queued-job cancellation.
Starting and running cancellation stays graceful while the orchestrator stops submission, drains Parsl work, and cleans owned resources.
After ``hard_cancel_after``, confirmed forced termination becomes ``lost`` because normal cleanup is not proven.
``finalizing`` and terminal states are not displaced by a late cancellation request.

Progress uses global sequence cursors.
``logs()`` uses byte offsets and snapshot identities internally so one read assembles a stable byte snapshot before decoding text.
Each public call returns the complete currently available combined text; the byte cursor is not part of the public API.
Transport loss does not change run state.
``RemoteWorkflowRun.open(transport, storage_path, run_id)`` reconstructs a handle without laptop-local claims about cluster control paths.

``result(destination=...)`` downloads to a private sibling, verifies the immutable manifest and every digest, and atomically installs the destination.
The exact destination may be reused only for the same verified bundle.
Record-owned and return-owned paths, including ``SharedArray`` backing data, become laptop-local assets.
Declared external cluster paths remain cluster ``Path`` values.
Historical result preparation addresses exact immutable records and never consults ``current.json``.

Transport failures expose stable codes for OpenSSH availability, connection, authentication, host keys, timeouts, SFTP transfer, remote protocol/validation, unsafe paths, and result integrity.
Launcher failures retain structured run errors, PSI/J uncertainty remains distinct, and failed, cancelled, lost, not-ready, and result-unavailable states keep their public exception types.
See :doc:`/gui/submitted_parsl` for the concise GUI action table.

Optional real-site smoke
------------------------

The deterministic test suite needs no external scheduler.
Maintainers may run ``tests/integration/parsl/test_cluster_smoke.py`` against an explicitly configured Slurm, PBS, or LSF site by setting ``BIOIMAGEFLOW_PSIJ_SMOKE_CONFIG`` to an absolute path to an untracked JSON file.
The file supplies exactly ``host``, ``staging_root``, ``remote_executable``, ``storage_path``, ``shared_runtime_root``, ``executor``, ``walltime_seconds``, ``timeout_seconds``, ``parsl_config_factory``, ``parsl_config_kwargs``, and ``executor_bindings``, with optional ``queue``, ``project``, and ``cpu_cores``.
No site value is inferred, embedded in the repository, or required by CI.
Use a unique staging root for the smoke and remove that fixture according to the site's transport-retention procedure after the terminal result has been verified.

API
---

.. currentmodule:: bioimageflow

.. autoclass:: ParslEngine
   :members:

.. autoclass:: ParslTaskPolicy
   :members:

.. autoclass:: LocalUpload
   :members:

.. autoclass:: PreLaunchScript
   :members:

.. autoclass:: SSHSubmissionTransport
   :members:

.. autoclass:: RemoteWorkflowRun
   :members:

.. autoclass:: PSIJLaunchConfig
   :members:

.. autoclass:: WorkerSlotCapacity
   :members:

.. autoclass:: ExecutorCapabilities
   :members:

.. autoclass:: WorkerEnvironmentAttestation
   :members:

.. autoclass:: ExecutorBinding
   :members:

.. autoclass:: ParslTaskError
   :members:

.. autoclass:: ParslConfigRef
   :members:

.. autoclass:: OrchestratorLaunchConfig
   :members:

.. autofunction:: submit_workflow

.. autoclass:: WorkflowRun
   :members:

.. autoclass:: BackendNotSupportedError

.. autoclass:: PSIJSubmissionUncertainError

.. autoclass:: WorkflowRunFailedError

.. autoclass:: WorkflowRunLostError

.. autoclass:: WorkflowRunNotReadyError

.. autoclass:: WorkflowRunResultUnavailableError

.. autoclass:: LauncherError

.. autoclass:: LauncherProtocolError

.. autoclass:: LauncherStateConflictError
