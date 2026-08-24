Run a Workflow on a Remote Cluster
==================================

This guide submits a workflow from a laptop to a Slurm cluster, saves its durable run ID, and reconnects to download the result.
PSI/J starts one BioImageFlow orchestrator job, and the existing Parsl backend starts the processing workers configured for the site.

Before you begin
----------------

You need:

- a Python workflow with a public ``images`` input;
- an absolute, versioned cluster Python environment containing compatible BioImageFlow, Parsl, PSI/J, the site's scheduler adapter, and the workflow's tool packages;
- an OpenSSH host alias for the cluster;
- a dedicated writable cluster directory visible at the same absolute path on login, orchestrator, and worker nodes; and
- the site's Slurm account, partition, worker node size, and scheduler limits.

Install managed cluster support on the laptop:

.. code-block:: bash

   pip install "bioimageflow[cluster]"

The laptop also needs the system ``ssh`` and ``sftp`` commands and the workflow's Python packages so it can build and prepare the workflow.
BioImageFlow uses the user's ordinary OpenSSH configuration for keys, agents, ports, jump hosts, and host-key checks.

Prepare the cluster Python
--------------------------

Create or ask the site administrator for a versioned virtual environment on the shared filesystem.
For example, prepare it on the cluster with the site's supported Python and package installer:

.. code-block:: bash

   python3 -m venv /shared/apps/bioimageflow/2026.08
   /shared/apps/bioimageflow/2026.08/bin/python -m pip install \
       "bioimageflow[parsl,psij]" \
       bioimageflow-common-tools \
       my-analysis-tools

Also install the PSI/J scheduler adapter required by the site if it is distributed separately.
Use an immutable or administratively controlled path and record how it was created; BioImageFlow attests the interpreter and installed distributions, but it does not own or update this environment.

The public API also accepts locked uv, Pixi, pylock, and wheelhouse descriptions.
Locked uv projects are captured and checked on the laptop, but target installation is not implemented yet; Pixi, pylock, and wheelhouse target realization are also not implemented.
Use ``from_existing_python()`` for an end-to-end managed run in this release.

Build a storage-independent workflow
------------------------------------

Define the workflow normally, without binding laptop input paths or remote runtime storage into its reusable definition:

.. code-block:: python

   from pathlib import Path

   from bioimageflow import Workflow
   from bioimageflow_common_tools import Files
   from my_analysis_tools import MeasureImage


   def build_workflow() -> Workflow:
       workflow = Workflow(name="measure-images")
       with workflow:
           images = workflow.input("images", Path, id="input-images")
           files = Files()(path=images, name="files")
           measurements = MeasureImage()(
               image=files["path"],
               name="measure",
           )
           workflow.output(
               "measurements",
               measurements["measurements"],
               id="output-measurements",
           )
       return workflow

Remote execution chooses storage beneath ``<cluster-root>/results/<workflow-id>`` by default.
Pass ``results_root=...`` to the cluster only when the site uses a separate durable results area.

Create the Parsl worker configuration
-------------------------------------

Create ``cluster/parsl.py`` with the site's ordinary Parsl executors and providers.
The factory receives a managed runtime, uses its generated worker initialization, and returns its executor bindings together with the live Parsl ``Config``:

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

Adjust the provider to the site's real node size, partition, account, limits, and network requirements.
``WorkerSlot`` states the resources guaranteed to one concurrent BioImageFlow task.
The additional environment attestation is the site's explicit claim that the selected Python already contains ``MeasureImage.environment``; add each distinct processing-tool environment offered by that executor.
The factory must use ``runtime.worker_init`` for every managed provider, return one matching binding for every executor label, leave ``retries=0``, and only construct configuration.

Describe and submit to the cluster
----------------------------------

Create the cluster and submit the workflow from a laptop script:

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

The managed PSI/J bridge currently accepts the scheduler, queue, project, walltime, and CPU count for the orchestrator job.
Leave ``SchedulerJob.memory``, ``SchedulerJob.gpu``, and ``SchedulerJob.attributes`` unset; those fields are rejected until the bridge can represent them without loss.
Parsl provider settings for worker allocations remain separate and may use the site's GPU or memory options.

``LocalUpload`` explicitly authorizes BioImageFlow to snapshot and transfer the laptop directory.
An ordinary absolute ``Path`` instead refers to data already available on the cluster, and a string always remains text.

The first submission creates the stable one-shot gateway and a content-addressed deployment beneath ``root``.
Later submissions with equivalent complete inputs reuse that deployment.
The returned run continues without the laptop after its durable ID is available.

Add site setup when necessary
-----------------------------

If Python, CUDA, compilers, or native libraries require Modules or Spack, add a non-interactive setup script:

.. code-block:: bash

   # cluster/setup.sh
   source /etc/profile
   module load python/3.12
   module load cuda/12.4
   source /shared/spack/share/spack/setup-env.sh
   spack load openslide

.. code-block:: python

   from bioimageflow.cluster import SetupScript

   cluster = RemoteCluster(
       host="my-hpc",
       root="/cluster/project/alice/bioimageflow",
       setup=SetupScript.from_file("cluster/setup.sh"),
       environment=ClusterEnvironment.from_existing_python(
           "/shared/apps/bioimageflow/2026.08/bin/python"
       ),
       parsl=parsl_configuration,
       orchestrator=orchestrator_job,
   )

BioImageFlow snapshots the script and sources the verified copy before bootstrap discovery, deployment, validation, the orchestrator, and every managed worker.
The script should expose site software, not install BioImageFlow or mutate a published deployment.

Validate and confirm explicitly
-------------------------------

For a GUI or confirmation screen, call the same lifecycle in phases:

.. code-block:: python

   connection = cluster.check_connection()
   deployment = cluster.deploy()
   prepared = cluster.prepare(
       build_workflow(),
       inputs={"images": LocalUpload(Path("images"))},
   )
   validation = cluster.validate(deployment=deployment)
   plan = cluster.plan(
       prepared,
       deployment=deployment,
       validation=validation,
   )

   show_confirmation(
       connection,
       deployment,
       prepared.manifest,
       validation,
       plan,
   )
   if validation.valid and user_confirmed():
       run = plan.submit()

``prepare()`` performs no network operation and owns immutable copies of invocation inputs.
``validate()`` and ``plan()`` do not create a workflow run, allocate workers, or submit a scheduler job.
Validation distinguishes login-node evidence from facts such as worker mount visibility and networking that can only be checked after allocation.

Close an abandoned prepared invocation or plan, or use it as a context manager, to release its laptop-side snapshots.
Do not create a second plan after an uncertain submission acknowledgement; recover the original preallocated ``plan.run_id``.
An attached run may remain ``prepared`` while launcher allocation is not yet observable; this does not authorize a second submission attempt.

Override a path stored in a node
--------------------------------

A reusable workflow should normally expose changing data through its public inputs.
For a workflow that stores an unconnected path directly in a node, inspect and override it for one invocation:

.. code-block:: python

   from bioimageflow import inspect_remote_node_paths

   for item in inspect_remote_node_paths(workflow).inputs:
       print(item.scoped_node_path, item.input_name)

   run = cluster.submit(
       workflow,
       node_input_overrides={
           "files": {"path": LocalUpload(Path("images"))},
       },
   )

Nested nodes use scoped paths such as ``preprocessing/files``.
Overrides apply only to unconnected path-shaped inputs and never mutate the original workflow.

Reconnect and download
----------------------

A new process needs only the host, root, and saved ID:

.. code-block:: python

   from pathlib import Path

   from bioimageflow.cluster import RemoteCluster

   cluster = RemoteCluster(
       host="my-hpc",
       root="/cluster/project/alice/bioimageflow",
   )
   run_id = Path("run-id.txt").read_text(encoding="utf-8").strip()
   run = cluster.attach(run_id)

   for event in run.progress(after_sequence=0):
       print(event)

   status = run.wait(poll_interval=5.0)
   if status == "succeeded":
       result = run.download_result(Path("downloads") / run.id)
   else:
       for diagnostic in run.diagnostics():
           print(diagnostic.scoped_node_path, diagnostic.message)

Result download verifies a portable bundle and publishes it atomically without silently replacing an unrelated destination.
Run-owned assets become local paths, while declared external cluster paths remain external values.

Cancel or clean up
------------------

Cancellation is idempotent and first requests cooperative workflow and Parsl cleanup:

.. code-block:: python

   if run.can_cancel:
       run.cancel()

Managed deployments, uploads, run records, and temporary material remain until explicit cleanup.
Preview exact candidates and consequences before deletion:

.. code-block:: python

   cleanup = cluster.plan_cleanup()
   show_cleanup_confirmation(cleanup)
   report = cluster.apply_cleanup(cleanup)

The default plan inventories abandoned temporary material at least one day old plus unreferenced deployment and upload objects.
To remove a retained run record, select terminal IDs explicitly with ``cluster.plan_cleanup(namespace="runs", run_ids=[run.id])``.
Cleanup refuses active runs, rechecks candidate identities and references, and reports changed candidates as skipped.
Transfer records are conservatively retained, live upload slots are not cleanup candidates, and cleanup of the workflow results tree, including an independent ``results_root``, is not implemented yet.

Next steps
----------

- :doc:`/reference/execution/remote_cluster` explains environment choices, deployment identity, operation effects, recovery, and security boundaries.
- :doc:`/reference/execution/routing` explains portable resource requirements and executor routing.
- :doc:`/reference/execution/monitoring` covers structured progress and node diagnostics.
