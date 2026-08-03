Run Work in Parallel
====================

BioImageFlow runs independent image-processing work concurrently by default.
For most local workflows, the main decision is simply how many worker processes each tool environment may use.

This guide starts with local execution.
Cluster execution is covered separately in :doc:`remote_cluster` because it requires a site profile in addition to a workflow.

What BioImageFlow can run concurrently
--------------------------------------

There are two useful kinds of parallel work:

- **Rows of one processing step.** If a source finds 100 images, a :class:`~bioimageflow_core.ProcessingTool` can process several image rows at once.
- **Independent branches.** If two processing steps depend only on the same source, BioImageFlow can run both branches while workers are available.

:class:`~bioimageflow_core.DataFrameTool` operations are small graph-shaping operations and run in the orchestrator process.
Their rows are not sent to workers.

Choose a local worker count
---------------------------

``max_workers`` is the default number of local worker processes for each tool environment:

.. code-block:: python

   from bioimageflow import Workflow, configure_wetlands

   configure_wetlands(root="./wetlands")

   with Workflow(storage_path="./results", max_workers=4) as workflow:
       images = files(path="./images", pattern="*.tif")
       masks = segment(image=images["path"], name="segment")
       result = workflow.compute(masks)

If ``images`` contains several rows, this workflow may process up to four rows at the same time.
The default is one worker per environment.

Start with a value that fits the machine and the tool.
CPU-heavy tools often benefit from several workers, while a tool that already uses all CPU threads internally may be faster with only one or two.
More workers also use more memory.

Set different counts for different environments
------------------------------------------------

Tools with different dependencies usually run in different worker environments.
You can size each environment independently:

.. code-block:: python

   with Workflow(storage_path="./results", max_workers=4) as workflow:
       workflow.get_environment(preprocess).max_workers = 8
       workflow.get_environment(segment).max_workers = 1

       images = files(path="./images", pattern="*.tif")
       prepared = preprocess(image=images["path"], name="preprocess")
       masks = segment(image=prepared["image"], name="segment")
       result = workflow.compute(masks)

Pass a processing-tool instance, an :class:`~bioimageflow_core.EnvironmentSpec`, or an environment name to :meth:`~bioimageflow.Workflow.get_environment`.
All tools that declare the same environment share that environment's worker pool.

Run independent branches
------------------------

Branches can overlap when neither depends on the other:

.. code-block:: python

   with Workflow(storage_path="./results", max_workers=4) as workflow:
       images = files(path="./images", pattern="*.tif")

       masks = segment(image=images["path"], name="segment")
       thumbnails = make_thumbnail(image=images["path"], name="thumbnail")

       result = workflow.compute(masks, thumbnails)

Both branches still obey the worker limit of their environment.
If they share one four-worker environment, they share those four workers; BioImageFlow does not create four workers per node.

Limit one processing node
-------------------------

A tool author may declare ``ResourceSpec(max_concurrent=...)``.
A workflow author may lower a particular node's concurrency without changing the shared tool class:

.. code-block:: python

   from bioimageflow import NodeResourceOverrides

   masks.set_resource_overrides(
       NodeResourceOverrides(max_concurrent=2)
   )

This node may then keep at most two row tasks active.
Other nodes using the same tool class are unchanged.
See :doc:`/reference/execution/resources` for all portable resource fields and validation rules.

GPU tools on a local machine
----------------------------

``ResourceSpec(gpu=1)`` describes what a task needs, but the local engine does not choose a GPU or assign ``CUDA_VISIBLE_DEVICES`` separately to each worker.
The worker processes inherit the environment in which their pool starts.

For one GPU, use one worker unless the tool documents that concurrent processes are safe.
For several GPUs, configure device visibility outside BioImageFlow or use a distributed executor that provides explicit GPU-capable worker slots.
See :doc:`/reference/execution/resources` for the guarantees made by each execution mode.

Debug without concurrency
-------------------------

Use sequential scheduling and one worker while investigating ordering or concurrency problems:

.. code-block:: python

   with Workflow(
       storage_path="./results",
       execution="sequential",
       max_workers=1,
   ) as workflow:
       ...

Switch back to the default parallel scheduling after the problem is understood.

Long-running native calls
-------------------------

``worker_timeout`` is a last-resort limit for a third-party native call that can deadlock:

.. code-block:: python

   workflow.get_environment(segment).worker_timeout = 600.0

Leave it as ``None`` unless you have a concrete need.
A normal slow task should be allowed to finish, while cancellation is the appropriate way to stop an entire workflow.

Next steps
----------

- :doc:`remote_cluster` runs the same workflow through a cluster profile.
- :doc:`/reference/execution/local` documents local worker ownership and lifecycle.
- :doc:`/reference/execution/resources` documents resource declarations and node overrides.
- :doc:`/reference/execution/index` compares every execution mode.
