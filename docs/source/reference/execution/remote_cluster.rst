Remote Cluster Execution
========================

This page explains how BioImageFlow runs a workflow on a Slurm, PBS, or LSF cluster when the submission starts from another computer, such as a laptop.

Most workflow users should not need to construct every value on this page.
A cluster administrator or the application that provides BioImageFlow should normally prepare a reusable cluster configuration.
Users then select that configuration, choose their inputs, confirm the run, and monitor it.

The complete journey of a remote run
------------------------------------

A remote run proceeds in these steps:

1. BioImageFlow connects to the cluster login node with the computer's normal OpenSSH configuration.
2. It uploads the workflow description and any inputs explicitly marked :class:`~bioimageflow.LocalUpload`.
3. A small BioImageFlow command on the login node validates the request.
4. BioImageFlow uses PSI/J to ask the cluster scheduler for one job that runs the orchestrator.
5. The orchestrator reads the workflow and uses Parsl to send ProcessingTool tasks to worker jobs.
6. Progress, diagnostics, and results are written to durable workflow storage on the cluster.
7. The laptop may disconnect and reconnect later using the cluster configuration, storage path, and run ID.

The laptop is not responsible for keeping the workflow alive after submission.

Why both PSI/J and Parsl are used
---------------------------------

Clusters usually have a scheduler such as Slurm, PBS, or LSF.
Users do not start processes directly on compute nodes; they submit jobs to the scheduler, which decides when and where those jobs run.

**PSI/J (Portable Submission Interface for Jobs) starts the BioImageFlow orchestrator.**
PSI/J gives BioImageFlow one Python API for submitting, observing, and cancelling that orchestrator job across the supported schedulers.
Without PSI/J, BioImageFlow would need separate launcher code for Slurm, PBS, and LSF.

**Parsl runs the workflow's processing tasks.**
Once the orchestrator is running, Parsl supplies tasks to one or more worker pools.
Its providers can ask the scheduler for CPU or GPU worker jobs and grow or shrink those pools according to the site's Parsl configuration.

BioImageFlow remains the workflow engine.
It decides which nodes need to run, resolves inputs, manages caching and provenance, and publishes results.
PSI/J and Parsl provide launch and task-execution services; they do not interpret the BioImageFlow graph.

.. code-block:: text

   laptop
      │  SSH: upload and one-shot commands
      ▼
   cluster login node
      │  PSI/J: submit one orchestrator job
      ▼
   BioImageFlow orchestrator
      │  Parsl: submit processing tasks
      ▼
   CPU and GPU worker pools

What “profile” means in these docs
----------------------------------

An **execution profile** or **cluster profile** is simply a saved group of public configuration values.
It is not a BioImageFlow class and there is no required profile file format.

A script might keep the values in a Python module.
A GUI might store them in its settings database under a name such as ``institute-slurm``.
The group commonly contains:

- an :class:`~bioimageflow.SSHSubmissionTransport` describing how to reach BioImageFlow on the cluster;
- a :class:`~bioimageflow.ParslConfigRef` naming the trusted function that builds the site's Parsl configuration;
- executor bindings and optional routes describing the worker pools;
- a :class:`~bioimageflow.PSIJLaunchConfig` describing the orchestrator job;
- an optional shared runtime path and task policy.

The word “profile” is only shorthand for passing these related values together.
See :doc:`submitted` for the Parsl function reference and :doc:`routing` for executor bindings.

Who normally configures what
----------------------------

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Person
     - Typical responsibilities
   * - Workflow user
     - Select a cluster configuration, select local or cluster inputs, choose the workflow storage location when appropriate, submit, monitor, cancel, and retrieve results.
   * - Application or GUI developer
     - Store the named configuration, call BioImageFlow's public validation and planning operations, present diagnostics, and retain run IDs.
   * - Cluster administrator or advanced profile author
     - Install the remote command, choose shared directories, define the trusted Parsl factory, configure worker initialization, describe executor capacities, and choose PSI/J scheduler settings.

Configure the SSH transport
---------------------------

:class:`~bioimageflow.SSHSubmissionTransport` tells BioImageFlow how to invoke its remote command and where temporary transfers belong:

.. code-block:: python

   from pathlib import PurePosixPath

   from bioimageflow import SSHSubmissionTransport

   transport = SSHSubmissionTransport(
       host="my-hpc",
       staging_root=PurePosixPath(
           "/cluster/project/my-workflow/transport"
       ),
       remote_executable=PurePosixPath(
           "/cluster/apps/bioimageflow/bin/bioimageflow-cluster-agent"
       ),
   )

``host``
   The OpenSSH destination.
   It may be an alias such as ``my-hpc`` from ``~/.ssh/config`` or a value such as ``alice@login.example.org``.
   OpenSSH remains responsible for keys, agents, ports, jump hosts, and host-key checks.

``staging_root``
   A writable directory on the cluster used as a transfer area.
   BioImageFlow puts incoming workflow bundles, uploaded input objects, operation receipts, and prepared result downloads there.
   Think of it as the remote inbox and transfer cache, not the workflow's results directory.
   It must be separate from workflow storage.
   Uploaded objects retained there must be visible to the orchestrator and workers that use them.

``remote_executable``
   The absolute cluster path to the ``bioimageflow-cluster-agent`` command installed with BioImageFlow.
   Despite its name, it is not a continuously running server.
   SSH starts it for one short request, such as validating a configuration, submitting a run, reading progress, or preparing a result download; it then exits.
   The path must already be executable on the login node without interactive shell setup.
   If even this command requires Modules or Spack initialization, an administrator may provide a stable executable wrapper at this path that performs the trusted setup and then executes ``bioimageflow-cluster-agent``.
   ``PreLaunchScript`` cannot perform this initial setup because it is handled later, after the remote command has validated the submission.

BioImageFlow invokes OpenSSH in batch mode.
It deliberately does not accept passwords, private-key contents, disabled host-key checking, or arbitrary SSH options as library values.

Understand the cluster paths
----------------------------

Several paths appear because they have different lifetimes and users:

.. list-table::
   :header-rows: 1
   :widths: 27 35 38

   * - Value
     - Purpose
     - Who must see it
   * - ``transport.staging_root``
     - Transfer inbox and content-addressed uploaded inputs
     - Login node, orchestrator, and workers that consume uploaded inputs
   * - ``workflow.storage_path``
     - Durable cache, progress, run state, diagnostics, and results
     - Orchestrator and all selected workers
   * - ``shared_runtime_root``
     - Verified runtime copies of tool code from archives when needed
     - Orchestrator and all workers using that code
   * - ``launch.work_dir``
     - Working directory in which PSI/J starts the orchestrator job
     - The scheduler service node for that job
   * - ``remote_executable``
     - Installed one-shot BioImageFlow command
     - Login node

All cluster paths are normalized absolute POSIX paths, for example ``/cluster/project/workflow/results``.
They refer to the cluster filesystem even when the Python code constructing them runs on a laptop.

Configure the orchestrator job
------------------------------

:class:`~bioimageflow.PSIJLaunchConfig` describes the one scheduler job that will run the orchestrator:

.. code-block:: python

   from datetime import timedelta
   from pathlib import PurePosixPath

   from bioimageflow import PSIJLaunchConfig

   launch = PSIJLaunchConfig(
       executor="slurm",
       walltime=timedelta(hours=2),
       queue="cpu",
       project="BIOIMAGE",
       cpu_cores=4,
       work_dir=PurePosixPath("/cluster/project/orchestrator"),
       hard_cancel_after=300,
   )

``executor`` selects the installed PSI/J plugin for ``slurm``, ``pbs``, or ``lsf``.
``queue`` is the scheduler partition or queue, and ``project`` is the scheduler account.
``walltime`` and ``cpu_cores`` apply to the orchestrator job only.
The Parsl configuration separately controls the potentially much larger worker jobs.

``hard_cancel_after`` is optional.
It permits forced scheduler cancellation if the orchestrator does not respond after a normal cancellation request and the grace period expires.

Test the configuration without submitting a job
-----------------------------------------------

Use :func:`~bioimageflow.validate_remote_execution_profile` for a “Test cluster configuration” action:

.. code-block:: python

   from bioimageflow import validate_remote_execution_profile

   report = validate_remote_execution_profile(
       transport=transport,
       parsl_config=parsl_config,
       executor_bindings=bindings,
       launch=launch,
       storage_path="/cluster/project/my-workflow/results",
   )

The operation connects to the login node and checks that:

- the remote command can run;
- the trusted Parsl factory can be imported and called there;
- required secret environment-variable names are available;
- the Config has ``retries=0`` and the expected executor labels;
- the PSI/J scheduler plugin is installed;
- the relevant path values and optional work directory are valid.

It does not create a workflow run, start workers, or submit a scheduler job.
It also cannot promise that a future queue will be available or that future worker nodes are correctly installed.
Runtime preflight verifies actual workers after allocation.

Initialize the orchestrator and workers
---------------------------------------

Cluster software often becomes available only after loading Environment Modules, Spack, Conda, or another site environment.
The orchestrator and workers are separate jobs, so they have separate initialization points.

**Orchestrator initialization** uses :class:`~bioimageflow.PreLaunchScript`:

.. code-block:: python

   from pathlib import Path

   from bioimageflow import PreLaunchScript

   pre_launch = PreLaunchScript.from_text(
       """\
       source /etc/profile.d/modules.sh
       module load python/3.12
       source /shared/spack/share/spack/setup-env.sh
       spack load py-bioimageflow
       """
   )

Use ``from_local_file(Path("cluster-init.sh"))`` when the script is on the submitting computer.
Use ``from_cluster_file("/shared/site/bioimageflow-init.sh")`` when the site already provides it on the cluster.
The cluster-file form may include ``expected_digest="sha256:..."`` to pin the expected content.

BioImageFlow verifies the script and gives PSI/J a read-only copy owned by the run.
It is sourced once before the orchestrator starts, so exported variables and directory changes persist in that process.

**Worker initialization** belongs in the trusted Parsl provider configuration:

.. code-block:: python

   from parsl import Config
   from parsl.executors import HighThroughputExecutor
   from parsl.providers import SlurmProvider

   def build_parsl_config(*, partition: str) -> Config:
       return Config(
           executors=[
               HighThroughputExecutor(
                   label="gpu-workers",
                   provider=SlurmProvider(
                       partition=partition,
                       worker_init="""\
                       source /etc/profile.d/modules.sh
                       module load cuda/12
                       source /shared/spack/share/spack/setup-env.sh
                       spack load py-bioimageflow-core
                       """,
                       walltime="02:00:00",
                   ),
               )
           ],
           retries=0,
       )

``worker_init`` runs for worker jobs, not the orchestrator.
It is trusted site configuration inside the importable factory, not an arbitrary string accepted from a remote workflow request.
BioImageFlow supports the site's initialization mechanism rather than giving special meaning to Spack or any other package manager.

Do not put credentials directly in either script.
Script files necessarily contain plaintext, and anything they print may enter scheduler-controlled logs.

Choose which paths are local and which are remote
-------------------------------------------------

BioImageFlow never guesses from the spelling of a value:

- ``LocalUpload(Path("./images"))`` means “read this laptop file or directory, copy it into the immutable submission, and install it on the cluster”;
- ``Path("/cluster/reference/atlas.tif")`` means “this path already exists on the cluster”;
- ``"/text/that/looks/like/a/path"`` remains an ordinary string.

``LocalUpload`` may be a path-like root input, a path-shaped item inside a root list or tuple, or an invocation-only node-input override.
For example:

.. code-block:: python

   inputs = {
       "files": [LocalUpload(first), LocalUpload(second)],
       "atlas": Path("/cluster/reference/atlas.tif"),
   }

The explicit wrappers preserve order and avoid ambiguous path heuristics.
Root DataFrames are transferred with Parquet and logical content digests; typed ``Path`` cells refer to normalized absolute cluster paths.

Override path inputs inside the graph
-------------------------------------

Some saved workflows contain an unconnected path directly on an internal node rather than exposing it through the workflow's root interface.
:func:`~bioimageflow.inspect_remote_node_paths` lists those editable path inputs with stable scoped node paths without reading files or contacting the cluster.

Pass invocation-only replacements with ``node_input_overrides``:

.. code-block:: python

   node_input_overrides = {
       "files": {
           "path": LocalUpload(Path("./images")),
       },
       "nested/masks": {
           "files": [LocalUpload(path) for path in selected_masks],
       },
   }

The outer key is the scoped workflow-node path and the inner key is the tool input name.
Only unconnected path-shaped constants or defaults may be replaced.
Connected inputs, workflow boundaries, unknown fields, and non-path parameters are rejected.

The replacement applies only to this invocation and does not mutate the caller's workflow.
BioImageFlow validates it on the laptop, copies explicit uploads, validates it again on the cluster, and applies installed cluster paths to the reconstructed graph.

Prepare exact bytes before user confirmation
--------------------------------------------

A GUI may need to validate inputs, show a summary, and ask the user to confirm before anything is sent to the cluster.
:func:`~bioimageflow.prepare_remote_submission` freezes the exact local bytes for that boundary:

.. code-block:: python

   from bioimageflow import prepare_remote_submission

   with prepare_remote_submission(
       workflow,
       inputs={"images": LocalUpload(Path("./images"))},
       targets=None,
       parsl_config=parsl_config,
       executor_bindings=bindings,
       launch=launch,
       pre_launch=PreLaunchScript.from_local_file(
           Path("cluster-init.sh")
       ),
       lifetime=1800,
   ) as prepared:
       show_confirmation(prepared.manifest.to_dict())
       run = prepared.submit(transport)

Preparation is local only.
It does not contact the cluster, create a workflow run, allocate workers, or submit a scheduler job.

The manifest lists relative paths, file sizes, and SHA-256 digests without exposing original laptop paths or secret values.
Submission consumes the prepared copies and never rereads the original local inputs or local pre-launch file.
The preparation may be submitted once; closing it, abandoning its context, or letting it expire cleans its temporary local state.

Cluster-resident pre-launch files are different because their bytes exist only on the cluster.
The manifest lists their cluster path and optional expected digest as an external source, and the remote command verifies and snapshots them before PSI/J submission.

Submit directly
---------------

When no separate confirmation screen is needed, :func:`~bioimageflow.submit_workflow` performs preparation and submission together:

.. code-block:: python

   from bioimageflow import LocalUpload, submit_workflow

   run = submit_workflow(
       workflow,
       inputs={"images": LocalUpload(Path("./images"))},
       parsl_config=parsl_config,
       executor_bindings=bindings,
       shared_runtime_root="/cluster/project/shared-runtime",
       launch=launch,
       pre_launch=pre_launch,
       transport=transport,
   )

Save ``run.id`` immediately.
The run continues independently of the laptop connection.

Reconnect, monitor, and retrieve results
----------------------------------------

A later process reconstructs the handle from three durable values:

.. code-block:: python

   from bioimageflow import RemoteWorkflowRun

   run = RemoteWorkflowRun.open(
       transport,
       "/cluster/project/my-workflow/results",
       saved_run_id,
   )

``progress()`` returns structured sequenced events.
``diagnostics()`` returns structured node failures.
``logs()`` returns human-readable orchestrator logs.
``wait()`` polls until a terminal state, and ``cancel()`` requests durable cancellation.

``export_result(destination)`` downloads a result bundle, verifies its manifest and every content digest, and installs the destination atomically.
Assets owned by the run become laptop-local paths, while declared external cluster paths remain cluster paths in the returned data.

Retry or recompute a retained run
---------------------------------

``run.plan_retry()`` previews a retry without changing the retained run:

.. code-block:: python

   plan = run.plan_retry()
   retry = run.start_retry(plan)

Selected recomputation uses ``RecomputeRequest`` and reports the cache entries that will be invalidated before confirmation; see :doc:`retries`.

The cluster creates the new run from retained workflow and input snapshots.
It does not reread laptop inputs.
If submission becomes uncertain, keep ``plan.retry_run_id`` and reconnect to that exact run instead of creating another retry.

Submission integrity
--------------------

Before asking PSI/J to submit externally, BioImageFlow writes an immutable intent describing the exact orchestrator job.
Immediately after PSI/J returns the scheduler's native job ID, BioImageFlow writes an immutable receipt.
Reconnection uses that receipt to observe or cancel the same job.

If the scheduler may have accepted a job but BioImageFlow could not save its receipt, the run remains ``prepared`` and raises :class:`~bioimageflow.PSIJSubmissionUncertainError`.
BioImageFlow does not automatically submit it again, because doing so might create a duplicate job.

Retention
---------

The workflow storage path is the source of truth for run state, cache records, diagnostics, and results.
The transport staging root is a supporting transfer area.

Operators may delete abandoned partial transfers, expired prepared result bundles, and uploaded objects no longer referenced by any retained run.
They must not remove an upload object while a retained run still needs it.
See :doc:`/reference/output_cache_storage` for the complete storage and retention contract.
