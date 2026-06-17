Planning and Cache State
========================

Hosts that want to render "this node is cached / prior selection miss / not
yet run / skipped" without executing anything use
:meth:`Workflow.plan <bioimageflow.Workflow.plan>`. Hosts that want to
clear selected cache records explicitly use
:meth:`Workflow.invalidate <bioimageflow.Workflow.invalidate>`.

The deeper cache model — result keys, selected records, and current
pointers — lives in :doc:`/concepts/caching`.

Workflow.plan()
---------------

``plan()`` returns ``dict[str, NodePlan]``, one entry per node:

.. code-block:: python

   plan = wf.plan()
   for name, entry in plan.items():
       print(name, entry.status, entry.final_result_key, entry.selected_record_id)

It uses the direct planning path — **no Wetlands worker pools are launched** and no tool code runs.

NodePlan
--------

Each :class:`~bioimageflow.engine.NodePlan` carries:

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Field
     - Description
   * - ``node_name``
     - Scoped name (``"outer/inner"`` for sub-workflow internals; plain
       name otherwise).
   * - ``final_result_key``
     - V1 result key when it can be computed from known selected upstream
       records. ``None`` for skipped and pending-upstream nodes.
   * - ``selected_record_id``
     - Selected record ID for ``final_result_key`` when the node is cached.
   * - ``status``
     - One of the :class:`NodePlanStatus` values below.
   * - ``upstream``
     - Tuple of scoped names of this node's direct upstreams.
   * - ``pending_upstreams``
     - Tuple of scoped upstream names whose selected records are not known yet.
   * - ``logical_signature``
     - Diagnostic logical signature. It is not the public cache identity; use
       ``final_result_key`` and ``selected_record_id`` for cache and provenance
       UI.
   * - ``cached``
     - Read-only bool; ``status is CACHED``.
   * - ``skipped``
     - Read-only bool; ``status is SKIPPED``.

NodePlanStatus → UI mapping
---------------------------

.. list-table::
   :header-rows: 1
   :widths: 18 50 32

   * - Status
     - Meaning
     - Suggested affordance
   * - ``CACHED``
     - The planned result key has a selected current record; ``compute()``
       would short-circuit.
     - Green / "up to date"
   * - ``PRIOR_SELECTION_MISS``
     - The planned result key has no selected record, but the same node has
       another selected current record. ``compute()`` would re-execute.
     - Yellow / "needs rebuild"
   * - ``UNEXECUTED``
     - No known cache record exists for this node yet.
     - Grey / "not yet run"
   * - ``SKIPPED``
     - The node is disabled, or has a disabled upstream that prevents
       execution. ``final_result_key`` and ``selected_record_id`` are empty.
     - Struck-through / muted
   * - ``PENDING_UPSTREAM``
     - At least one consumed upstream selected record is not known until that
       upstream executes, so the final result key cannot be determined yet.
     - Grey / "pending upstream"

Sub-workflow aggregation
------------------------

A sub-workflow node aggregates its internals: the parent reports
``CACHED`` only when *every* internal entry is cached, and
``UNEXECUTED`` otherwise. Internal entries are still present in the
plan dict under their scoped names, so a host can show drill-down
("3/5 internals cached") on a "needs rebuild" parent.

Workflow.invalidate()
---------------------

When a long-lived host changes a parameter and wants to clear cached
results explicitly, :meth:`Workflow.invalidate
<bioimageflow.Workflow.invalidate>` removes cache selections for
the named nodes:

.. code-block:: python

   cleared = wf.invalidate(["segment"])               # cascade=True default
   cleared_nodes = {selection.node_name for selection in cleared}

   wf.invalidate(["segment"], cascade=False)         # just "segment"

Returns ``InvalidatedSelection`` entries for actual ``current.json`` pointers that were removed.
Each entry includes the node name, result key, selected record ID when readable, and a removal status.
Entries that had no selected cache record are silently skipped.
``KeyError`` is raised when any name is unknown.

.. warning::

   ``invalidate()`` is **not safe** to call concurrently with
   ``compute()``. The library does not currently expose a public lock
   primitive; coordinate externally (cancel + join + invalidate).

Cycle handling
--------------

The two methods diverge on cycles:

- ``Workflow.validate()`` reports a cycle as a
  :class:`~bioimageflow.validation.ValidationError` with
  ``kind="cycle"``. **Non-raising.**
- ``Workflow.plan()`` raises
  :class:`~bioimageflow.engine.CycleInWorkflowError` — it cannot
  topologically order a cyclic graph and refuses to fabricate a plan.

The recommended pattern: call ``validate()`` first, gate ``plan()`` on
"no cycle errors":

.. code-block:: python

   errs = wf.validate()
   if any(e.kind == "cycle" for e in errs):
       render_cycle(errs)
   else:
       render_plan(wf.plan())

Specs.md §6.5 covers the contract end-to-end.
