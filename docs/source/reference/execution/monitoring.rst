Progress, Diagnostics, and Cancellation
=======================================

BioImageFlow exposes the same logical node progress and failure information for local, attached Parsl, and submitted execution.
Submitted handles persist that information so it remains available after reconnect.

Attached progress callbacks
---------------------------

Pass an ``on_progress`` callback when constructing a workflow or through the supported execution surface:

.. code-block:: python

   def show_progress(event):
       print(event.node_name, event.status, event.row, event.total_rows)

   workflow = Workflow(
       storage_path="./results",
       on_progress=show_progress,
   )

The common statuses are ``started``, ``row_complete``, ``completed``, and ``cached``.
Failed events may carry a :class:`~bioimageflow.NodeFailureDiagnostic`.

Parsl futures may complete out of order, but row-complete callbacks for one node are serialized and emitted in aligned row order.
Events from independent nodes may interleave.
A whole-node ``process_batch()`` emits no row-complete events.

Submitted progress
------------------

Submitted runs persist globally sequenced entries:

.. code-block:: python

   cursor = 0
   for entry in run.progress(after_sequence=cursor):
       cursor = entry["sequence"]
       handle(entry)

Save the last consumed sequence to resume without parsing or deduplicating log text.
Remote progress reads bounded pages internally and returns the currently available entries after the cursor.

Structured node failures
------------------------

:class:`~bioimageflow.NodeFailureDiagnostic` contains:

- ``scoped_node_path``;
- stable ``category`` and ``exception_type`` values;
- a sanitized message and optional sanitized traceback;
- optional attempt identity;
- retry status and terminal status.

Use ``run.diagnostics()`` after reconnect:

.. code-block:: python

   for failure in run.diagnostics():
       print(failure.scoped_node_path)
       print(failure.category, failure.exception_type)
       print(failure.message)

Failures from concurrently executing nodes are persisted independently.
The same logical diagnostic is available from attached failure events and from local or remote submitted-run inspection.
Consumers do not need to parse exception strings, logs, or private run artifacts.

Secret values discovered through environment-variable secret references are redacted from structured messages and tracebacks.
Applications should still avoid placing credentials in tool arguments, filenames, pre-launch scripts, or arbitrary exception text.

Parsl task diagnostics
----------------------

Detailed backend task records are stored separately from immutable cache records:

.. code-block:: text

   diagnostics/v1/runs/<run-id>/nodes/<node-key>/<invocation-id>/tasks/<task-id>.json

They record task correlation, executor label, mode, retry identity, row positions, tool origin, status, timestamps, and terminal error type.
A task becomes terminal only after BioImageFlow observes its future.
Task diagnostics do not contribute to result keys or record IDs.

When several Parsl tasks fail, BioImageFlow chooses the primary raised exception by compiled workflow and task position rather than completion timing.
:class:`~bioimageflow.ParslTaskError` includes the selected node, task, executor, attempt, row position, retry identity, exception type, message, and remote traceback identity.

Attached cancellation
---------------------

Call :meth:`~bioimageflow.Workflow.cancel` from another thread while a workflow is active.
Cancellation stops new submissions, requests cancellation of outstanding engine-owned futures, ignores late results, drains writers and submitted futures, and leaves incomplete attempts unselected.
The cancellation context is cleared when execution finishes, so a later compute does not inherit the request.

Closing a ``compute_steps()`` iterator has the same cleanup obligations for work already submitted by that iterator.

Submitted cancellation
----------------------

Call ``WorkflowRun.cancel()`` or ``RemoteWorkflowRun.cancel()``.
The request is durable and retry-safe.
An active orchestrator observes it, calls ``Workflow.cancel()``, drains its Parsl work, and publishes ``cancelled`` after cleanup.

A configured ``hard_cancel_after`` permits forced termination of the exact persisted process or scheduler job after the grace period.
Confirmed forced termination becomes ``lost`` rather than claiming graceful cleanup.
Terminal or finalizing runs are not displaced by a late cancellation request.

Logs
----

``run.logs()`` returns the currently persisted orchestrator stdout and stderr for human troubleshooting.
Remote reading assembles stable byte snapshots before decoding replacement text.
Logs are not a structured progress, diagnostic, status, or result API.

Attached result export
----------------------

An attached Direct, Wetlands, or Parsl execution can produce the same run-specific verified result bundle as submitted execution:

.. code-block:: python

   context = WorkflowExecutionContext()
   result = workflow.compute(target, run_context=context)
   exported = context.export_result(
       result,
       destination=downloads / context.run_id,
   )

The successful context retains the run ID, target binding, and engine-neutral provider outcomes needed to distinguish record-owned, return-owned, and external assets.
Export uses a private sibling, verifies the complete bundle, and installs the destination atomically.
It creates no new workflow execution and allocates no engine resources.
Attached export snapshots the supplied successful result when ``export_result()`` is called, so it is process-local and should run before the caller mutates the DataFrame or removes transient assets.
