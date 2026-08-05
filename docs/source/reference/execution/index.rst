Execution Reference
===================

BioImageFlow keeps graph planning, cache publication, progress, and provenance independent of the system that performs processing work.
The execution mode determines where :class:`~bioimageflow_core.ProcessingTool` tasks run and who owns their resources.

The main names used in this section
-----------------------------------

You do not need prior knowledge of Parsl, PSI/J, or cluster schedulers to read this section.
These names describe different jobs:

**BioImageFlow orchestrator**
   The Python process that understands the workflow.
   It decides which nodes need to run, handles DataFrames and caching, records progress, and publishes results.

**Worker**
   A process that performs the image-processing work for one or more :class:`~bioimageflow_core.ProcessingTool` tasks.

**Parsl**
   An optional Python task-execution library used by BioImageFlow for distributed execution.
   The orchestrator gives processing tasks to Parsl, and Parsl sends them to configured worker pools called *executors*.

**PSI/J**
   A Python interface for starting and controlling jobs through cluster schedulers such as Slurm, PBS, and LSF.
   BioImageFlow uses PSI/J to start one orchestrator job on a cluster; it does not use PSI/J for each workflow node.

**Cluster scheduler**
   The site service, such as Slurm, that queues jobs and assigns cluster machines to them.

**Execution profile**
   A convenient name for a saved group of execution settings, such as the cluster address, Parsl configuration, executor descriptions, and PSI/J launch settings.
   ``Profile`` is not a BioImageFlow class.
   A script, application, or GUI may store these public values in its own configuration format and give the group a name such as ``my-cluster``.

Choose an execution mode
------------------------

.. list-table::
   :header-rows: 1
   :widths: 22 28 50

   * - Mode
     - Use it for
     - Configuration
   * - Local
     - Normal workstation and single-machine execution
     - The default engine; choose local worker counts
   * - Direct
     - Focused tests and debugging tiny tools
     - Explicit ``engine="direct"``; no isolated workers
   * - Attached Parsl
     - An application that already controls its Python process and may control a Parsl runtime
     - Pass a Parsl ``Config`` or caller-owned DataFlowKernel and public executor bindings
   * - Submitted local
     - A reconnectable run in a separate process on the same machine
     - Pass a trusted :class:`~bioimageflow.ParslConfigRef` and local launch configuration
   * - Submitted cluster-local
     - Code already running on a cluster login node
     - Pass a PSI/J launch configuration directly
   * - Submitted remote
     - A laptop or service submitting to a cluster through SSH
     - Add :class:`~bioimageflow.SSHSubmissionTransport` and use explicit uploads

Start with :doc:`/how-to/run_in_parallel` for ordinary local work or :doc:`/how-to/remote_cluster` for a first remote submission.

The execution layers
--------------------

It helps to distinguish three layers:

1. The **BioImageFlow orchestrator** compiles the graph, runs DataFrame tools, selects cache records, and publishes results.
2. An **execution engine** runs ProcessingTool work locally or submits it to Parsl executors.
3. In submitted cluster mode, a **launcher** starts one reconnectable orchestrator process; Parsl providers then obtain worker resources.

PSI/J does not replace Parsl and Parsl does not replace the BioImageFlow orchestrator.
The launcher starts the orchestrator, while Parsl executes processing tasks selected by that orchestrator.

Scheduling terms
----------------

``execution="parallel"`` allows independent processing nodes to overlap.
``execution="sequential"`` permits one processing node and one unfinished row task at a time.
``execution="workflow"`` on a Parsl engine follows the workflow's saved scheduling choice.

DataFrame tools and recursive workflow boundaries always execute in the orchestrator.
Processing tools execute in local environment workers or Parsl worker slots.

Reference map
-------------

.. toctree::
   :maxdepth: 1

   local
   resources
   attached_parsl
   routing
   submitted
   retries
   results
   remote_cluster
   monitoring
   api

Optional dependencies
---------------------

Local execution is installed with ``bioimageflow`` and does not require Parsl or PSI/J.
Attached and submitted-local Parsl execution require ``bioimageflow[parsl]``.
PSI/J cluster launch additionally requires ``bioimageflow[psij]`` and the site scheduler's PSI/J executor plugin in the launch environment.
Remote submission uses the system OpenSSH ``ssh`` and ``sftp`` clients.

:func:`~bioimageflow.get_execution_capabilities` reports which modes and public integration contracts are available without eagerly importing optional Parsl or PSI/J dependencies.
