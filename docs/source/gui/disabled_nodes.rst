Disabled Nodes
==============

Hosts often need to **temporarily skip** part of a graph — to debug a
failing step in isolation, to render a "what would the rest of the
pipeline look like without this node" preview, or to A/B test two
arrangements without re-typing them. BioImageFlow exposes this through
a per-node ``enabled`` flag.

Toggling enabled
----------------

Three equivalent surfaces:

.. code-block:: python

   node.disable()                  # node-level
   node.enable()

   wf.disable(node_or_name, ...)   # workflow-level, varargs
   wf.enable(node_or_name, ...)

   sess.set_enabled("node", False) # session-level (wire-format edit)
   sess.set_enabled("node", True)

The session form mirrors the wire-format clean-form rule (see
:doc:`wire_format`): re-enabling strips the ``enabled`` key entirely.

Skip propagation
----------------

A disabled node — and **every node transitively downstream of it** — is
skipped during execution. ``Workflow.plan()`` reflects this with
``NodePlanStatus.SKIPPED`` (see :doc:`planning_and_cache`); the
``logical_signature`` of skipped nodes is the empty string.

Skipping does not cascade to *parents*. A disabled leaf node leaves its
ancestors untouched.

The ``enabled`` flag is **not** part of result identity
----------------------------------------------------------

Re-enabling a previously disabled node hits the existing cache — no
recompute. The flag affects scheduling, not the per-node identity.
This is an intentional design choice: toggling visibility on a node
should not invalidate its cached output.

compute_steps
-------------

For hosts that need fine-grained control over execution
(stepping through nodes manually, gating compute on UI events),
:meth:`Workflow.compute_steps <bioimageflow.Workflow.compute_steps>`
yields a :class:`~bioimageflow.engine.NodeStep` for every node in
topological order — **including skipped ones**:

.. code-block:: python

   for step in wf.compute_steps(targets):
       print(step.node_name, step.skipped)
       if not step.skipped:
           step.execute()

Each step exposes:

- ``step.node_name`` — scoped name (matches ``NodePlan.node_name``).
- ``step.skipped`` — bool; ``True`` when disabled or downstream of a
  disabled node.
- ``step.execute()`` — runs the node and returns the resulting
  DataFrame; raises :class:`~bioimageflow.engine.DisabledNodeError`
  when called on a skipped step. The host typically branches on
  ``step.skipped`` rather than catching the exception.

Calling ``step.execute()`` is mandatory before advancing to the next
step (the engine checks); skipping it leaves the workflow in an
inconsistent state.

How compute() handles skipped targets
-------------------------------------

:meth:`Workflow.compute` raises
:class:`~bioimageflow.engine.DisabledNodeError` only when **all
requested targets** are skipped. Multi-target calls silently omit
skipped targets from the result and return DataFrames for the
executable ones:

.. code-block:: python

   wf.compute(a, b)        # a is skipped, b is not — returns just b's result
   wf.compute(a)           # a is skipped — DisabledNodeError

This matches the host's expectation that toggling one node off should
not break a multi-target compute.

Wire format
-----------

The enabled flag round-trips through the wire format as
``"enabled": false`` on disabled nodes; the key is **omitted** for
enabled nodes. See :doc:`wire_format`.

Worked example: a "skip this node" toggle
-----------------------------------------

A GUI exposes a per-node "skip" checkbox. The handler updates the
session, re-validates, and re-renders the cache plan:

.. code-block:: python

   def on_toggle_skip(name: str, skip: bool):
       sess.set_enabled(name, not skip)
       errs = sess.validate()
       plan = sess.plan()             # SKIPPED entries propagate
       render(sess, errs, plan)

Because ``set_enabled`` is non-structural (see :doc:`workflow_session`),
the toggle does not re-resolve any tool class — even on a workflow with
hundreds of nodes.
