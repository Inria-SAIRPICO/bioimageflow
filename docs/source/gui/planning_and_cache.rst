Planning and Cache State
========================

Hosts that want to render "this node is cached / out of date / not yet
run / skipped" without executing anything use
:meth:`Workflow.plan <bioimageflow.Workflow.plan>`. Hosts that want to
clear cache directories explicitly use
:meth:`Workflow.invalidate <bioimageflow.Workflow.invalidate>`.

The deeper signature-hash model — what feeds the hash, how cache
directories are named — lives in :doc:`/concepts/caching`.

Workflow.plan()
---------------

``plan()`` returns ``dict[str, NodePlan]``, one entry per node:

.. code-block:: python

   plan = wf.plan()
   for name, entry in plan.items():
       print(name, entry.status, entry.sig_hash[:12])

It instantiates an in-process :class:`DefaultEngine` with
``use_wetlands=False`` — **no Wetlands worker pools are launched**, no
tool code runs. The hashes returned are byte-identical to what
``compute()`` would compute.

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
   * - ``sig_hash``
     - The signature hash, byte-identical to what ``compute()`` would
       produce. Empty string for ``SKIPPED`` nodes.
   * - ``status``
     - One of the four :class:`NodePlanStatus` values below.
   * - ``upstream``
     - Tuple of scoped names of this node's direct upstreams.
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
     - Current signature hash matches an existing cache entry;
       ``compute()`` would short-circuit.
     - Green / "up to date"
   * - ``OUT_OF_DATE``
     - The storage directory contains entries from previous runs, but
       none match the current hash. ``compute()`` would re-execute.
     - Yellow / "needs rebuild"
   * - ``UNEXECUTED``
     - No storage directory exists for this node yet — it has never
       run.
     - Grey / "not yet run"
   * - ``SKIPPED``
     - The node is disabled, or has a disabled upstream that prevents
       execution. ``sig_hash`` is empty.
     - Struck-through / muted

Sub-workflow aggregation
------------------------

A sub-workflow node aggregates its internals: the parent reports
``CACHED`` only when *every* internal entry is cached, and
``UNEXECUTED`` otherwise. Internal entries are still present in the
plan dict under their scoped names, so a host can show drill-down
("3/5 internals cached") on an "out of date" parent.

Workflow.invalidate()
---------------------

When a long-lived host changes a parameter and wants to clear cached
results explicitly, :meth:`Workflow.invalidate
<bioimageflow.Workflow.invalidate>` removes the cache directories of
the named nodes:

.. code-block:: python

   cleared = wf.invalidate(["segment"])               # cascade=True default
   # cleared == {"segment", "measure", "summary"}

   wf.invalidate(["segment"], cascade=False)         # just "segment"

Returns the set of node names whose directories were actually removed
(entries that didn't exist on disk are silently skipped). ``KeyError``
is raised when any name is unknown.

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
