Retry a Submitted Run
=====================

A submitted run can be reopened after your Python process exits.
Once it reaches ``succeeded``, ``failed``, ``cancelled``, or ``lost``, you can create a new run from the exact retained submission without rebuilding the workflow or rereading uploaded files.

Retry unfinished work
---------------------

Open the retained run, inspect its plan, and start it after confirmation:

.. code-block:: python

   from bioimageflow import WorkflowRun

   previous = WorkflowRun.open(storage_path, saved_run_id)
   plan = previous.plan_retry()
   print("new run:", plan.retry_run_id)
   retry = previous.start_retry(plan)

The retry has a new run ID, and ``retry.parent_id`` identifies ``previous.id``.
Successful cached nodes are reused, while failed, cancelled, or unfinished work runs again.
The retained graph, invocation, targets, custom tools, and node-input overrides are cloned from the previous run.
Run-owned input and bootstrap trees are copied, while verified content-addressed uploads are retained and reused; laptop source paths are never read again.

Recompute selected nodes
------------------------

Use :class:`~bioimageflow.RecomputeRequest` when cached work must run again:

.. code-block:: python

   from bioimageflow import RecomputeRequest

   plan = previous.plan_retry(
       RecomputeRequest(
           ("preprocessing/normalize",),
           cascade=True,
       )
   )

   for selected in plan.invalidations:
       print(selected.node_path, selected.record_id)

   retry = previous.start_retry(plan)

Scoped paths use ``/`` for nested workflows.
With ``cascade=True``, BioImageFlow also selects downstream cache pointers; with ``cascade=False``, it selects only the named nodes.
Immutable cache records remain retained: recomputation removes only the mutable current selections.

Preparing is a preview: it creates no run, changes no cache selection, starts no worker, and submits no scheduler job.
Submission checks the retained submission and staged-byte digests and the parent status revision.
When recomputation is requested, it also checks the complete cache-selection revision and exact invalidation preview.
It refuses the operation if another attached or submitted execution is active or if confirmed material changed.

Confirm now and submit later
----------------------------

The plan is strict JSON-safe data and can cross a service restart:

.. code-block:: python

   from bioimageflow import RunRetryPlan, WorkflowRun

   saved_plan = plan.to_dict()

   # Later, possibly in a new process:
   previous = WorkflowRun.open(storage_path, saved_plan["parent_run_id"])
   retry = previous.start_retry(RunRetryPlan.from_dict(saved_plan))

``start_retry()`` is idempotent for the child ID and complete plan.
If scheduler submission has an uncertain outcome, keep its planned run ID and reconnect to that ID; never submit it again automatically.

Retry a remote run
------------------

The same calls work through a :class:`~bioimageflow.RemoteWorkflowRun`:

.. code-block:: python

   from bioimageflow import RemoteWorkflowRun

   previous = RemoteWorkflowRun.open(transport, cluster_storage, saved_run_id)
   plan = previous.plan_retry(
       RecomputeRequest(("segmentation/model",), cascade=True)
   )
   retry = previous.start_retry(plan)

The cluster performs the preview, revision checks, journaled invalidation, retained invocation cloning, run-owned byte copying, content-addressed upload reuse, and launch.
The caller does not inspect remote launcher files or issue SSH filesystem commands.
``RemoteWorkflowRun.open()`` restores both ``parent_id`` and ``retry_plan`` for a retained child.
