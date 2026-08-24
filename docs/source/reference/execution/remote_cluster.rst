Remote Cluster Execution
========================

BioImageFlow can deploy and run a workflow on a Slurm, PBS, or LSF cluster from a laptop with SSH access.
The managed interface has one common path:

.. code-block:: text

   describe cluster -> build workflow -> submit -> save run ID -> reconnect -> download result

:class:`~bioimageflow.cluster.RemoteCluster` owns deployment, validation, planning, submission, reconnection, and transfer paths beneath one cluster ``root``.
Users do not install a cluster agent, assemble staging paths, or make the Parsl configuration importable on the cluster themselves.

PSI/J submits the small scheduler job that runs the BioImageFlow orchestrator.
The orchestrator then uses the existing Parsl backend to send processing tasks to worker jobs.
BioImageFlow remains responsible for graph semantics, caching, routing, provenance, progress, retries, diagnostics, and results.

Prerequisites
-------------

The laptop needs BioImageFlow's cluster support and the system ``ssh`` and ``sftp`` clients:

.. code-block:: bash

   pip install "bioimageflow[cluster]"

The cluster account must provide:

- non-interactive OpenSSH access under the user's normal SSH configuration;
- a dedicated writable directory visible at the same absolute path from the login node, orchestrator, and workers;
- permission for the orchestrator allocation to submit Parsl worker jobs;
- a compatible Python interpreter, directly or after the setup script runs; and
- the site's scheduler client and required drivers or system libraries.

For managed uv, BioImageFlow builds a verified environment around that interpreter from wheels captured on the laptop.
For ``from_existing_python()``, the selected absolute interpreter must already contain compatible BioImageFlow, Parsl, PSI/J, the scheduler adapter, and workflow tool packages, and BioImageFlow attests it before use.
BioImageFlow does not download or install Python itself.
It does not install scheduler services, drivers, privileged libraries, or change cluster policy.

Golden path: a pre-provisioned Python
-------------------------------------

A typical laptop project contains its workflow and one Parsl factory, while the cluster has a versioned environment managed by the user or site:

.. code-block:: text

   cell-study/
   |-- images/
   |-- workflow.py
   |-- run_cluster.py
   `-- cluster/
       `-- parsl.py

The workflow should describe reusable computation, without a laptop input path or cluster result directory.
Remote runtime storage defaults to ``<cluster.root>/results/<workflow-id>``.

The Parsl factory remains ordinary programmable Python.
It returns the live Parsl configuration and BioImageFlow executor bindings together:

.. code-block:: python

   from parsl import Config
   from parsl.executors import HighThroughputExecutor
   from parsl.providers import SlurmProvider

   from bioimageflow.cache import compute_env_hash
   from bioimageflow.parsl import (
       ExecutorBinding,
       ParslFactoryResult,
       WorkerEnvironmentAttestation,
       WorkerSlot,
   )
   from my_analysis_tools import MeasureImage


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
       managed = runtime.executor_binding(
           slot=WorkerSlot(cpu=1, memory="4 GB"),
       )
       tool_environment = MeasureImage.environment
       binding = ExecutorBinding(
           label="cpu-workers",
           environments=managed.environments + (
               WorkerEnvironmentAttestation(
                   name=tool_environment.name,
                   dependency_hash=compute_env_hash(
                       tool_environment.dependencies
                   ),
                   allow_flexible_versions=(
                       tool_environment.allow_flexible_versions
                   ),
                   core_requirement=runtime.core_requirement,
               ),
           ),
           capabilities=managed.capabilities,
       )
       return ParslFactoryResult(
           config=Config(executors=[executor], retries=0),
           executor_bindings={"cpu-workers": binding},
       )

``runtime.worker_init`` applies the selected setup script and activates the exact deployment in every managed worker.
Every provider that starts a worker shell must use it.
:class:`~bioimageflow.parsl.WorkerSlot` describes the resources guaranteed to one concurrent BioImageFlow task, not the whole scheduler node.
Each additional :class:`~bioimageflow.WorkerEnvironmentAttestation` declares that the activated worker environment contains that processing-tool environment.
For managed uv, its packages must be part of the captured locked closure; for an existing Python, it is an explicit site claim.
Add every distinct tool environment offered by the executor; planning fails closed if a node has no compatible attestation.

The complete laptop-side submission is:

.. code-block:: python

   from datetime import timedelta
   from pathlib import Path

   from bioimageflow.cluster import (
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
       environment=ClusterEnvironment.from_existing_python(
           "/shared/apps/bioimageflow/2026.08/bin/python"
       ),
       parsl=ParslConfiguration.from_file(
           "cluster/parsl.py",
           factory="build",
           kwargs={"account": "BIOIMAGE"},
       ),
       orchestrator=SchedulerJob(
           scheduler="slurm",
           queue="compute",
           project="BIOIMAGE",
           walltime=timedelta(hours=4),
           cpu=4,
       ),
   )

   run = cluster.submit(
       build_workflow(),
       inputs={"images": LocalUpload(Path("images"))},
   )
   Path("run-id.txt").write_text(f"{run.id}\n", encoding="utf-8")

   run.wait()
   if run.status == "succeeded":
       result = run.download_result(Path("results"))
   else:
       for diagnostic in run.diagnostics():
           print(diagnostic.scoped_node_path, diagnostic.message)

Save ``run.id`` as soon as submission returns.
The scheduler run continues if the laptop process exits or loses its network connection.

Describe the cluster
--------------------

``RemoteCluster`` needs only ``host`` and ``root`` to attach to a retained run.
Deployment and submission additionally require an environment, Parsl configuration, and orchestrator job.

``host``
   An OpenSSH destination such as an alias from ``~/.ssh/config`` or ``alice@login.example.org``.
   OpenSSH owns users, keys, agents, ports, jump hosts, and host-key policy.

``root``
   A dedicated absolute cluster directory owned by BioImageFlow for gateway publications, deployments, content objects, operation receipts, run state, transfers, results, and temporary installation material.
   Internal paths are implementation details and are never assembled by user code.

``results_root``
   An optional absolute shared path that replaces the default ``<root>/results`` base.

``environment``
   The Python environment used by the orchestrator and ordinary managed workers.
   Choose a content-owned locked uv project or an externally managed absolute interpreter.

``parsl``
   The trusted source and arguments for the factory that constructs the Parsl configuration and bindings.

``orchestrator``
   The scheduler request for the BioImageFlow orchestrator only.
   Parsl providers separately request worker resources.
   The current PSI/J bridge represents scheduler, queue, project, walltime, and CPU count; non-null memory, GPU, and custom attributes are rejected rather than silently dropped.

``setup``
   An optional non-interactive Bash script that exposes site-managed Python, Modules, Spack packages, CUDA, compilers, or native libraries before deployment activation.

Choose an environment source
----------------------------

:class:`~bioimageflow.cluster.ClusterEnvironment` describes environment sources with an explicit ownership boundary.
``from_uv_project()`` and ``from_existing_python()`` currently realize usable target deployments.

.. list-table::
   :header-rows: 1
   :widths: 23 30 27 20

   * - Situation
     - Constructor
     - Required inputs
     - Current status
   * - Locked uv project
     - ``from_uv_project()``
     - ``pyproject.toml``, ``uv.lock``, selected local sources
     - Supported with a wheel-complete closure and a pre-existing compatible target Python
   * - Locked Pixi project
     - ``from_pixi_project()``
     - Pixi manifest, ``pixi.lock``, environment name
     - Target realization is not implemented
   * - Standard Python lock
     - ``from_pylock()``
     - ``pylock.toml`` and optional local project
     - Target realization is not implemented
   * - Offline cluster
     - ``from_wheelhouse()``
     - Exact lock and every compatible wheel
     - Target realization is not implemented
   * - Site-managed Python
     - ``from_existing_python()``
     - Absolute versioned interpreter path
     - Supported end to end

For example:

.. code-block:: python

   uv_environment = ClusterEnvironment.from_uv_project(
       ".",
       groups=("cluster",),
   )
   pixi_environment = ClusterEnvironment.from_pixi_project(
       ".",
       environment="workflow",
   )
   locked_environment = ClusterEnvironment.from_pylock(
       "pylock.toml",
       project=".",
   )
   offline_environment = ClusterEnvironment.from_wheelhouse(
       "wheelhouse",
       lock="pylock.toml",
   )
   site_environment = ClusterEnvironment.from_existing_python(
       "/shared/apps/bioimageflow/2026.08/bin/python"
   )

Managed uv verifies the frozen lock, builds local projects into universal wheels on the laptop, captures locked registry wheels and compatible pinned uv installer wheels, and performs an offline target installation.
Every selected package must have a captured wheel compatible with the cluster; target-side sdist builds are not supported.
Local project wheels must be platform-independent ``py3-none-any`` or ``py2.py3-none-any`` wheels.
The cluster Python must already exist and satisfy the frozen Python requirement because the deployment never downloads Python.
Installer capture currently reads public PyPI, and registry-wheel capture uses unauthenticated immutable HTTP(S) URLs; ``auth_refs`` are not used for private registry capture yet.
Pixi, pylock, and standalone wheelhouse target realization remain unavailable and fail before scheduler submission.
An existing Python environment is attested and validated but remains an externally managed dependency, so changing it can invalidate a deployment between operations.

Expose site software with a setup script
----------------------------------------

:class:`~bioimageflow.cluster.SetupScript` replaces remote pre-launch scripts because the same initialization must apply during bootstrap, installation, validation, the orchestrator, and workers:

.. code-block:: python

   from bioimageflow.cluster import SetupScript

   setup = SetupScript.from_file("cluster/setup.sh")

Local text and file sources are snapshotted before contact with the cluster.
Use ``SetupScript.from_text(...)`` for a small generated script.
An administrator-managed cluster file must be pinned:

.. code-block:: python

   setup = SetupScript.from_cluster_file(
       "/shared/site/bioimageflow-setup.sh",
       sha256="sha256:0123456789abcdef...",
   )

The script should be safe to source repeatedly and should expose site software rather than install packages or mutate a published deployment.
Do not put literal credentials in it.

Define the Parsl configuration
------------------------------

:meth:`~bioimageflow.cluster.ParslConfiguration.from_file` snapshots one local Python file and explicitly listed supporting files or packages.
No parent directory is uploaded implicitly.
``kwargs`` contains finite JSON-safe values, while ``secret_refs`` maps factory argument names to cluster environment-variable names:

.. code-block:: python

   parsl = ParslConfiguration.from_file(
       "cluster/parsl.py",
       kwargs={"account": "BIOIMAGE"},
       secret_refs={"registry_token": "REGISTRY_TOKEN"},
       include=("cluster/helpers.py",),
   )

Secret values are resolved only on the cluster and are never part of serialized manifests or structured diagnostics.
The factory must return exactly :class:`~bioimageflow.parsl.ParslFactoryResult`, use ``runtime.worker_init`` for managed providers, declare one binding per executor label, and set Parsl ``retries=0``.
It may construct Parsl executors and providers but must not load a DataFlowKernel or submit jobs.

An advanced ``ParslConfiguration.from_module("module:function", ...)`` refers to factory code already installed in an externally managed environment.

Choose local uploads and cluster paths explicitly
-------------------------------------------------

BioImageFlow never guesses path meaning from spelling or existence:

- ``LocalUpload(Path("images"))`` snapshots a laptop file or directory for transfer;
- ``Path("/cluster/reference/atlas.tif")`` refers to an existing absolute cluster path; and
- a string remains ordinary text even when it resembles a path.

``LocalUpload`` can appear in root path inputs, lists or tuples of path inputs, and ``node_input_overrides``.
Use :func:`~bioimageflow.inspect_remote_node_paths` to discover unconnected path-shaped values inside a workflow, then replace them for one invocation:

.. code-block:: python

   node_input_overrides = {
       "files": {"path": LocalUpload(Path("images"))},
       "preprocessing/masks": {
           "files": [LocalUpload(path) for path in selected_masks],
       },
   }

Overrides use the same scoped node paths as planning and diagnostics.
They never mutate the reusable workflow.

Use the explicit lifecycle for confirmation
-------------------------------------------

``cluster.submit()`` composes deployment, immutable preparation, validation, planning, upload, and scheduler submission.
A GUI or service can expose each consequential boundary:

.. code-block:: python

   deployment = cluster.deploy()
   prepared = cluster.prepare(
       workflow,
       inputs={"images": LocalUpload(selected_directory)},
   )
   validation = cluster.validate(deployment=deployment)
   plan = cluster.plan(
       prepared,
       deployment=deployment,
       validation=validation,
   )

   show_confirmation(
       deployment,
       prepared.manifest,
       validation,
       plan,
   )
   if validation.valid and user_confirmed():
       run = plan.submit()

``prepare()`` is local and freezes the workflow, invocation, and every explicit laptop upload.
``deploy()`` freezes local environment, setup, Parsl, project, and BioImageFlow bootstrap inputs before its first network operation.
Changing an original path after its corresponding snapshot cannot alter the confirmed submission.

``validate()`` runs the deployed factory and verifies the environment, bindings, scheduler adapter, paths, secrets, and retry policy without submitting a scheduler job.
Its report distinguishes verified login-node facts, declarations, and facts that require a real worker allocation.

``plan()`` uses the same cache, resource, compatibility, and route logic as execution.
It creates no workflow run or workers and binds the exact deployment, prepared invocation, validation evidence, scheduler request, node routes, and preallocated run ID.
``plan.submit()`` is the plan's only mutation and is idempotent for that logical attempt.

:class:`~bioimageflow.cluster.PreparedClusterInvocation` and :class:`~bioimageflow.cluster.RemoteExecutionPlan` own local resources, support context management, expire, and should be closed when abandoned.
A serialized copy is a detached summary: it remains useful for display and persistence but cannot recreate omitted local bytes.
After remote mutation begins, recover through ``cluster.attach(plan.run_id)`` instead of creating a second attempt.
During an uncertain acknowledgement, attachment may report ``prepared`` until launcher allocation becomes observable; that state is not permission to submit a new plan.

Reconnect, observe, cancel, and download
----------------------------------------

A later process needs only the SSH destination, cluster root, and run ID:

.. code-block:: python

   from pathlib import Path

   from bioimageflow.cluster import RemoteCluster

   cluster = RemoteCluster(
       host="my-hpc",
       root="/cluster/project/alice/bioimageflow",
   )
   run_id = Path("run-id.txt").read_text(encoding="utf-8").strip()
   run = cluster.attach(run_id)

   print(run.status)
   for event in run.progress():
       print(event)

   if run.can_cancel:
       run.cancel()

   run.wait()
   if run.result_available:
       result = run.download_result(Path("results"))

``progress()`` returns structured sequenced events, and ``diagnostics()`` returns independent structured node failures.
Cancellation is idempotent and first requests cooperative cleanup; a configured hard-cancellation grace may later cancel retained scheduler jobs.
Result download verifies the portable bundle and every content digest before publishing the destination atomically.
Run-owned assets become local paths, while declared external cluster paths remain external values.

Retry a retained run
--------------------

Remote retries preserve the existing confirmation contract:

.. code-block:: python

   retry_plan = run.plan_retry()
   retry = run.start_retry(retry_plan)

The cluster clones the retained invocation and reuses verified content-addressed uploads without rereading laptop paths.
Selected recomputation uses :class:`~bioimageflow.RecomputeRequest`; see :doc:`retries`.

Cleanup retained state explicitly
---------------------------------

Managed deployments, uploaded objects, run records, transfers, and results are never silently evicted.
Preview exact cleanup candidates and consequences before applying them:

.. code-block:: python

   cleanup = cluster.plan_cleanup()
   show_cleanup_confirmation(cleanup)
   report = cluster.apply_cleanup(cleanup)

With no filter, the plan inventories abandoned temporary material at least one day old and unreferenced deployment and upload objects.
Use ``namespace="temporary"`` with ``older_than_seconds`` from one day through one year to narrow temporary cleanup.
Run records are candidates only when their terminal IDs are passed explicitly with ``namespace="runs", run_ids=[...]``.

The gateway signs the exact inventory and binds it to the root, gateway publication, candidate identities, and retained-run and transfer reference revision.
Applying the plan revalidates those facts, skips changed candidates, and refuses active runs or state referenced by retained runs.
Deleting a terminal run record explicitly gives up attachment, diagnostics, and retry history, but does not delete the workflow results tree.
Transfer deletion and independent ``results_root`` cleanup are not implemented; transfer records are conservatively retained, live upload slots are not inventoried, and result storage must be managed separately.

Operation effects
-----------------

.. list-table::
   :header-rows: 1
   :widths: 27 15 17 14 13 14

   * - Operation
     - Contacts cluster
     - Writes remote state
     - Scheduler job
     - Creates run
     - Workers
   * - ``check_connection()``
     - Yes
     - No
     - No
     - No
     - No
   * - ``deploy()``
     - Yes
     - If absent
     - No
     - No
     - No
   * - ``prepare()``
     - No
     - No
     - No
     - No
     - No
   * - ``validate()``
     - Yes
     - Temporary only
     - No
     - No
     - No
   * - ``plan()``
     - Maybe
     - Temporary only if validation refreshes
     - No
     - No
     - No
   * - ``plan.submit()`` or ``cluster.submit()``
     - Yes
     - Yes
     - One orchestrator
     - Yes
     - Later through Parsl
   * - ``attach()`` and inspection
     - Yes
     - No
     - No new job
     - No new run
     - No new workers
   * - ``cancel()``
     - Yes
     - Run state
     - May cancel retained jobs
     - No new run
     - No new workers
   * - ``download_result()``
     - Yes
     - Bounded transfer
     - No
     - No
     - No
   * - ``plan_cleanup()``
     - Yes
     - No
     - No
     - No
     - No
   * - ``apply_cleanup()``
     - Yes
     - Deletes selected state
     - No
     - No
     - No

Validation and planning never allocate a test worker or submit a scheduler job implicitly.
Submitting the orchestrator may begin consuming the user's allocation immediately, and Parsl providers may request additional worker allocations later.
A successful login-node validation is not a site acceptance test; run a small representative workflow before relying on a new scheduler, queue, mount, or worker-network configuration for production data.

Security and recovery boundaries
--------------------------------

Setup scripts and Parsl factories are trusted executable code selected by the user.
BioImageFlow snapshots their bytes, reports their digests, rejects unsafe path forms, and redacts secret values, but it cannot prevent trusted code or cluster administrators from reading accessible secrets or causing undeclared effects.

Published deployments and content objects are content-addressed and checked before reuse.
The gateway uses bounded one-shot SSH requests rather than a daemon, and attachment never upgrades it implicitly.
Gateway-classified operation failures report a stable category, operation phase, allocation state, retry safety, safe next action, and sanitized diagnostic.
Unexpected dependency and child-process failures can still surface through a bounded generic diagnostic, so applications must also handle an unclassified operation failure.

An uncertain submission must be recovered using the original plan and preallocated run ID.
Do not create a new plan merely because an acknowledgement was lost: the scheduler may already have accepted the orchestrator job.
