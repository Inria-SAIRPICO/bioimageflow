Retained-Run Retry and Recomputation
====================================

The retry contract is shared by submitted-local and submitted-remote execution.
It creates a child run from retained material rather than reconstructing an invocation from current application state.

Ordinary retry
--------------

Every terminal submitted run can plan and start a child execution:

.. code-block:: python

   plan = run.plan_retry()
   inspect(plan)
   retry = run.start_retry(plan)

The child receives a fresh run ID and retains ``parent_id`` and ``retry_plan`` after reconnect.
An ordinary retry preserves current cache selections, so successful work is reused and remaining work is attempted again.
The child clones the parent's exact materialized workflow payload, invocation, targets, node-input overrides, custom-tool sources, pre-launch artifact, routing, and launch configuration.
Run-owned ``inputs/`` and ``bootstrap/`` trees are copied into the child, while verified content-addressed upload objects are retained and reused by their installed paths.
It never rebuilds that meaning from caller objects and never rereads an original ``LocalUpload`` source.

``succeeded``, ``failed``, ``cancelled``, and ``lost`` are retryable.
Nonterminal states are rejected.
A retry of ``cancelled`` or ``lost`` reuses only selections durably published before termination; incomplete attempts are not current cache records.

Scoped recomputation
--------------------

:class:`~bioimageflow.RecomputeRequest` names recursive scoped nodes and chooses whether invalidation cascades downstream:

.. code-block:: python

   plan = run.plan_retry(
       RecomputeRequest(("analysis/segment",), cascade=True)
   )

``plan.invalidations`` is the exact non-mutating preview of current cache pointers selected for removal.
``cascade=True`` includes transitive downstream nodes; ``cascade=False`` selects only the named scope.
Immutable record directories are never deleted.

Plan identity
-------------

:class:`~bioimageflow.RunRetryPlan` binds:

- parent and child run IDs;
- parent terminal state and status revision;
- canonical storage path;
- canonical retained submission digest;
- retained ``inputs/`` and ``bootstrap/`` material digest and entry count;
- full cache-selection revision, used when recomputation is requested;
- optional recompute request and exact selected cache pointers;
- active-run conflicts observed during preview;
- a canonical SHA-256 digest over the complete plan.

Every submission uses the same schema, with ``retry_plan`` set to ``null`` for an original run or the complete strict plan for a retry child.
No resolved secret or credential enters the plan, retained submission, representation, or diagnostic.

Allocation and concurrency
--------------------------

Planning briefly takes the storage allocation guard for a consistent read but creates no run, invalidates nothing, starts no DataFlowKernel or worker, and submits no scheduler job.
``plan.conflicting_run_ids`` reports active executions observed during planning.

Submission takes the same guard, refuses any active attached or submitted execution, and verifies the storage binding, parent revision, and retained submission and material digests.
For recomputation, it also verifies the cache-selection revision and exact preview.
It atomically allocates the child, durably journals each selected-pointer invalidation, and only then starts the retained launch configuration.
Replaying confirmation after an interruption completes a partially journaled invalidation and dispatches only when no durable backend launch marker exists.
An ambiguous launch marker is never treated as permission to resubmit.
Stale plans fail with :class:`~bioimageflow.WorkflowRunRetryError`; callers must prepare a new plan and confirm it again.

Restart-safe confirmation
-------------------------

The plan is JSON-safe and is the only confirmation object:

.. code-block:: python

   payload = plan.to_dict()

   # After a process restart:
   plan = RunRetryPlan.from_dict(payload)
   parent = WorkflowRun.open(plan.storage_path, plan.parent_run_id)
   child = parent.start_retry(plan)

Remote code uses ``RemoteWorkflowRun.open(...)`` and the same ``start_retry(plan)`` method.
Remote preview and mutation are bounded public cluster-agent operations; callers never inspect launcher storage or issue arbitrary filesystem commands.

Submission uncertainty
----------------------

The plan owns one deterministic child ID, and the retained child owns the complete plan, making duplicate protocol delivery idempotent.
If transport or scheduler submission may have succeeded, BioImageFlow never authorizes automatic resubmission.
Reconnect to ``plan.retry_run_id`` and inspect its durable state.
