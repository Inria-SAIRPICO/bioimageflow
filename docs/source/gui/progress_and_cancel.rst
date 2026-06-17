Progress and Cancellation
=========================

Hosts driving a long-running ``compute()`` need two things: a stream of
progress events to render, and a Cancel button that actually stops the
workflow. BioImageFlow exposes both through stable, thread-safe APIs.

ProgressEvent
-------------

The engine fires a :class:`~bioimageflow.ProgressEvent` for every
node-level transition and every per-row update. Pass a callback at
workflow construction:

.. code-block:: python

   from bioimageflow import Workflow, ProgressEvent

   def on_progress(ev: ProgressEvent):
       print(ev.node_name, ev.status, ev.row, ev.total_rows)

   wf = Workflow(on_progress=on_progress)

Fields:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Field
     - Description
   * - ``node_name``
     - Scoped node name (sub-workflow scope included).
   * - ``status``
     - One of ``"started"``, ``"row_progress"``, ``"row_complete"``,
       ``"completed"``, ``"cached"``, ``"failed"``, ``"cancelled"``.
   * - ``result_key``
     - V1 result key when the event is tied to a cacheable node result.
   * - ``record_id``
     - Selected record ID for ``"completed"`` and ``"cached"`` events.
   * - ``row``
     - Index of the current row (per-row events).
   * - ``total_rows``
     - Total rows in this node's input (per-row events).
   * - ``message``
     - Optional sub-row text (e.g., from
       ``RemoteTaskHandle.update``).
   * - ``current``
     - Optional sub-row progress numerator.
   * - ``maximum``
     - Optional sub-row progress denominator.
   * - ``timestamp``
     - Wall-clock seconds-since-epoch.

``result_key`` and ``record_id`` are the only cache identities exposed by
progress events. Diagnostic signatures are separate debug values and are not
cache keys. During a cache miss, ``"started"`` can include
``result_key`` while ``record_id`` remains empty until publication succeeds.

The engine **serializes callbacks via an internal lock** — your
``on_progress`` callable does not need to be thread-safe. It runs on
the engine's progress dispatch path; expensive work in the callback
will slow down execution, so push UI updates onto a dispatch queue
rather than re-rendering inline.

Sub-row progress
----------------

Tools that drive ``RemoteTaskHandle.update(message=, current=, maximum=)``
inside ``process_row`` produce ``status="row_progress"`` events with
``message`` / ``current`` / ``maximum`` populated. Use these for tools
with a clear inner loop (training epochs, tile counts, optimizer
iterations); a one-row tool with no inner progress simply omits them.

Workflow.cancel() from a GUI
----------------------------

:meth:`Workflow.cancel <bioimageflow.Workflow.cancel>` flips a
thread-safe flag. The engine checks it before each node and between
rows; in-flight rows have ``task.cancel()`` called on their Wetlands
handle (cooperative — see :doc:`/tutorials/cancellation`).

The classic GUI wiring:

.. code-block:: python

   import threading
   from bioimageflow.engine import WorkflowCancelledError

   def run_compute():
       try:
           wf.compute(target)
       except WorkflowCancelledError:
           pass    # render the partial state — see below

   thread = threading.Thread(target=run_compute, daemon=True)
   thread.start()

   def on_cancel_button():
       wf.cancel()
       thread.join()

The flag is reset at the start of each ``compute()`` call, so calling
``wf.cancel()`` *before* the next compute starts is harmless.

Cooperative tool-side check
---------------------------

Long-running ``ProcessingTool.process_row`` implementations should opt
in to cooperative cancellation by accepting the ``task`` keyword
argument and checking ``task.cancel_requested`` at safe points. The
full pattern is documented in :doc:`/tutorials/cancellation`; this
page only re-states that the engine is the side that *requests*
cancellation — the tool is the side that *honours* it.

Handling WorkflowCancelledError
-------------------------------

The engine raises :class:`~bioimageflow.engine.WorkflowCancelledError`
from ``compute()`` once the cancel flag is observed. Hosts catch it
where they call ``compute()``:

- already-cached nodes are untouched — the cache survives;
- partially-completed nodes leave their cache entries in their previous
  state (the new entry is not committed);
- subsequent ``plan()`` calls reflect the post-cancel state, so the GUI
  can re-render "needs rebuild" / "unexecuted" indicators correctly.

A typical handler:

.. code-block:: python

   try:
       result = wf.compute(target)
   except WorkflowCancelledError:
       render_cancelled_state(wf.plan())
