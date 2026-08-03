Run a Workflow on a Remote Cluster
==================================

BioImageFlow can submit a workflow from a laptop to a Slurm, PBS, or LSF cluster and reconnect to it later.
One scheduler job starts the BioImageFlow orchestrator; Parsl then starts the workers that process workflow rows.

This guide assumes that you already have:

- a saved workflow with a public input named ``images``;
- an OpenSSH host alias for the cluster;
- a writable shared storage directory on the cluster;
- a site profile module containing a trusted Parsl configuration, executor bindings, SSH transport, and PSI/J launch settings.

The first two items are ordinary user inputs.
A cluster administrator or an application can define the reusable site profile by following :doc:`/reference/execution/remote_cluster`.

Install the required components
-------------------------------

On the laptop, install BioImageFlow with Parsl support:

.. code-block:: bash

   pip install "bioimageflow[parsl]"

The laptop also needs the system ``ssh`` and ``sftp`` commands.

On the cluster, install BioImageFlow, Parsl, PSI/J, and the PSI/J executor plugin for the site's scheduler:

.. code-block:: bash

   pip install "bioimageflow[parsl,psij]"

Load the workflow and site profile
----------------------------------

The workflow's storage path is the path seen by the cluster orchestrator and workers, not a laptop directory:

.. code-block:: python

   from pathlib import Path

   from bioimageflow import Workflow
   from my_cluster_profile import (
       executor_bindings,
       launch,
       parsl_config,
       shared_runtime_root,
       transport,
   )

   workflow = Workflow.load(
       "workflow.json",
       storage_path=Path("/cluster/project/my-workflow/results"),
   )

The profile values are runtime configuration and are not stored in ``workflow.json``.
This keeps the workflow portable between local execution and different clusters.

Test the profile without submitting a job
-----------------------------------------

Validate cluster connectivity, the trusted Parsl factory, secret references, executor labels, and relevant paths before submission:

.. code-block:: python

   from bioimageflow import validate_remote_execution_profile

   report = validate_remote_execution_profile(
       transport=transport,
       parsl_config=parsl_config,
       executor_bindings=executor_bindings,
       launch=launch,
       storage_path=workflow.storage_path.as_posix(),
   )

   if not report.valid:
       for diagnostic in report.diagnostics:
           print(diagnostic.code, diagnostic.message)
       raise SystemExit("The cluster profile is not ready")

This operation contacts the cluster but does not create a workflow run, start a Parsl DataFlowKernel, or submit a scheduler job.

Upload inputs and submit
------------------------

Mark laptop files explicitly with :class:`~bioimageflow.LocalUpload`:

.. code-block:: python

   from pathlib import Path

   from bioimageflow import LocalUpload, submit_workflow

   run = submit_workflow(
       workflow,
       inputs={"images": LocalUpload(Path("./images"))},
       parsl_config=parsl_config,
       executor_bindings=executor_bindings,
       shared_runtime_root=shared_runtime_root,
       launch=launch,
       transport=transport,
   )

   print("Run ID:", run.id)

BioImageFlow snapshots and verifies uploaded bytes before scheduler submission.
The confirmed run consumes that immutable snapshot rather than reading the original laptop path again.
An ordinary ``Path`` means a path that already exists on the cluster, and a string always remains a string.

Initialize the orchestrator environment when needed
---------------------------------------------------

Some clusters require a module, Spack environment, virtual environment, or site script before Python can start BioImageFlow.
Pass one typed pre-launch script beside the launch configuration:

.. code-block:: python

   from bioimageflow import PreLaunchScript

   pre_launch = PreLaunchScript.from_text(
       """\
       source /etc/profile.d/modules.sh
       module load python/3.12
       source /shared/spack/share/spack/setup-env.sh
       spack load py-bioimageflow
       """
   )

   run = submit_workflow(
       workflow,
       inputs={"images": LocalUpload(Path("./images"))},
       parsl_config=parsl_config,
       executor_bindings=executor_bindings,
       shared_runtime_root=shared_runtime_root,
       launch=launch,
       pre_launch=pre_launch,
       transport=transport,
   )

Use :meth:`~bioimageflow.PreLaunchScript.from_local_file` for a script stored beside the submitting application.
Use :meth:`~bioimageflow.PreLaunchScript.from_cluster_file` for a cluster-resident site script, optionally with an expected SHA-256 digest.

BioImageFlow always gives PSI/J a verified, read-only copy owned by the run.
The script is sourced once before the orchestrator starts, so its exports and directory changes persist in the orchestrator process.
It does not initialize Parsl workers; worker modules, containers, and GPU libraries belong in the Parsl provider's worker initialization.

Watch, reconnect, and retrieve the result
-----------------------------------------

The submitting process may exit after saving the run ID.
A later process can open the same run:

.. code-block:: python

   from pathlib import Path, PurePosixPath

   from bioimageflow import RemoteWorkflowRun

   run = RemoteWorkflowRun.open(
       transport,
       PurePosixPath("/cluster/project/my-workflow/results"),
       saved_run_id,
   )

   for event in run.progress(after_sequence=0):
       print(event)

   status = run.wait(poll_interval=5.0)
   if status == "succeeded":
       result = run.result(destination=Path("./downloads") / run.id)
   elif status == "failed":
       for diagnostic in run.diagnostics():
           print(diagnostic.scoped_node_path, diagnostic.message)

Progress cursors and diagnostics are structured public data; an application does not need to parse logs.
``run.logs()`` remains useful for human troubleshooting.

Cancel a run
------------

Call ``run.cancel()`` from either the original or a reconnected handle.
BioImageFlow first requests cooperative cancellation so the orchestrator can stop new work and clean up.
If the profile defines ``hard_cancel_after`` and the orchestrator does not respond, a later observation may terminate the exact persisted scheduler job.

Next steps
----------

- :doc:`/reference/execution/remote_cluster` defines site profiles, immutable preparation, storage, and PSI/J behavior.
- :doc:`/reference/execution/submitted` explains submitted runs independently of SSH transport.
- :doc:`/reference/execution/monitoring` covers progress, diagnostics, cancellation, and reconnect semantics.
