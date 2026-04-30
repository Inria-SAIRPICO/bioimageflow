Overview
========

The pages in this tree describe the surface BioImageFlow exposes for
**hosts** — graphical editors, web servers, plugin frameworks. A host
typically:

- holds a long-lived **session** that mirrors the workflow as a
  wire-format dict;
- mutates that dict in response to user edits (drag a node, change a
  field, toggle a checkbox);
- materializes a :class:`~bioimageflow.Workflow` on demand to validate,
  plan, or compute;
- renders validation errors and cache state inline, without crashing on
  partial graphs.

Audience
--------

This tree is **not** for workflow scripts or tool authors. The Workflow
Authors tree (:doc:`/concepts/index`, :doc:`/tutorials/index`) covers
how to build a DAG and write a tool. Pages here assume you are writing
the host that *carries* those workflows.

Reading order
-------------

The pages are designed to be read in order:

1. :doc:`wire_format` — the JSON shape every other page relies on.
2. :doc:`from_dict` — the load-time API and its validation flag matrix.
3. :doc:`workflow_session` — the editing model: when to materialize,
   when to mutate in place.
4. :doc:`tool_registry` — installing, registering, and indexing tool
   packages.
5. :doc:`live_validation` — keystroke-rate validation helpers.
6. :doc:`planning_and_cache` — rendering "cached / out-of-date /
   unexecuted / skipped" without executing.
7. :doc:`disabled_nodes` — the disable/enable flag and partial
   execution.
8. :doc:`progress_and_cancel` — wiring progress callbacks and a Cancel
   button.
9. :doc:`tool_schemas` — schemas for inline form widgets.

Mental model
------------

A useful sketch for what a host typically does:

.. code-block:: text

   ┌────────────────┐    add/remove/edit     ┌───────────────────┐
   │  Editor state  │ ─────────────────────> │ WorkflowSession   │
   │  (dict-shaped) │                        │  (dict + caches)  │
   └────────────────┘                        └─────────┬─────────┘
                                                       │
                                       to_workflow() / validate() / plan()
                                                       │
                                                       ▼
                                        ┌──────────────────────────┐
                                        │  Workflow                │
                                        │  validate() → errors     │
                                        │  plan() → NodePlan map   │
                                        │  compute() (or steps)    │
                                        └──────────────────────────┘

Pages further down the list refine each box.
