WorkflowSession
===============

:class:`~bioimageflow.WorkflowSession` is the editing model for hosts
that mutate a workflow incrementally — typically once per keystroke or
drag event. The session holds the wire-format dict as canonical state
and materializes a :class:`~bioimageflow.Workflow` on demand, caching
the build across edits.

Why a separate class
--------------------

:class:`Workflow` builds nodes eagerly in ``__init__``: column bindings
and upstream references are wired during construction. Retrofitting
incremental mutation on top would require invasive changes to ``Node``.
A dict-backed session, materialized to a Workflow only when needed, is
both simpler and matches what hosts actually want to send over the
wire.

Construction
------------

.. code-block:: python

   from bioimageflow import WorkflowSession

   sess = WorkflowSession()                  # empty graph
   sess = WorkflowSession(data)              # load existing wire format
   sess = WorkflowSession(data, registry=reg, storage_path="./bif_data")

The ``registry`` argument is stored with the session for host-side tool resolution.
The ``storage_path`` argument injects (or overrides)
``data["config"]["storage_path"]`` without mutating the input dict.

Mutating operations
-------------------

Each mutation operates on the underlying dict; the session deep-copies
the input so callers can keep their own references.

.. code-block:: python

   sess.add_node({"name": "files", "tool_module": "...", "tool_class": "Files", ...})
   sess.add_edge({"from": "files", "to": "threshold",
                  "column": "path", "field": "image", "id": "e1"})
   sess.set_constant("threshold", "cutoff", 100.0)
   sess.set_enabled("filter", False)
   sess.remove_edge("e1")
   sess.remove_node("filter")

- ``add_node`` raises ``ValueError`` if the name already exists.
- ``add_edge`` requires ``from``, ``to``, ``column``, ``field``.
- ``remove_edge`` raises ``KeyError`` if ``id`` is unknown.
- ``set_constant`` runs the value through
  :func:`~bioimageflow.validation.serialize_constant` and stores the
  envelope in the node's ``constants`` dict.
- ``set_enabled(name, True)`` strips the ``enabled`` key entirely
  (clean-form rule); ``False`` writes ``"enabled": false``.

Structural vs non-structural edits
----------------------------------

This is the **load-bearing contract** of the session. The two edit
classes have different cache invalidation semantics:

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Edit
     - Effect on ``_workflow_cache``
   * - ``add_node`` / ``remove_node``
     - Cache **dropped**. Next ``to_workflow()`` rebuilds.
   * - ``add_edge`` / ``remove_edge``
     - Cache **dropped**. Next ``to_workflow()`` rebuilds.
   * - ``set_constant``
     - Cache **mutated in place**. Tool class is **not** re-resolved.
   * - ``set_enabled``
     - Cache **mutated in place**. Tool class is **not** re-resolved.

Tool resolution is the slow operation (importing the module, walking
the package, scope-stamping classes). Session edits that only change
constants or the enabled flag avoid that cost — a constant slider can
fire ``set_constant`` plus ``validate()`` per frame without rebuilding
the graph.

Materialization
---------------

The materialized workflow is reused across non-structural edits, validation is cached until a compute-affecting edit, and planning refreshes storage-facing cache state:

- :meth:`~bioimageflow.WorkflowSession.to_workflow` — returns the
  built :class:`Workflow`. Internally calls
  ``Workflow.from_dict(partial=True, validate_only=True,
  auto_install=False)`` so per-node failures are captured rather than
  raised.
- :meth:`~bioimageflow.WorkflowSession.validate` — returns
  ``list[ValidationError]``: the union of build-time errors
  (``wf.errors``) and ``wf.validate()`` results, deduplicated.

:meth:`~bioimageflow.WorkflowSession.plan` returns ``dict[str, NodePlan]`` with the same shape as ``Workflow.plan()``.
It reuses the materialized workflow object when possible, but refreshes the storage/current snapshot on every call so external ``compute()`` or ``invalidate()`` operations are reflected immediately.

A fourth method, :meth:`~bioimageflow.WorkflowSession.to_dict`, returns
a deep copy of the wire format — a snapshot suitable for export or for
sending over the wire.

Read-only views
---------------

- ``sess.nodes`` — ``dict[name, dict]`` of node entries (deep-copied
  per access).
- ``sess.edges`` — ``list[dict]`` of edge entries (deep-copied per
  access).
- ``sess.errors`` — cached errors from the last
  :meth:`validate()` call.
- ``sess.failed_nodes`` — failed nodes from the last
  :meth:`to_workflow()` build (empty until ``to_workflow`` runs).

Failed nodes are not exceptional
--------------------------------

Hosts should treat ``failed_nodes`` as a normal part of the editing
loop — every paste of a graph that references an uninstalled tool will
populate it. The right response is to render the failure inline (red
node, tooltip with ``ValidationError.message``) rather than refuse to
load.

The session deliberately uses ``auto_install=False`` so that adding a
node referencing an uninstalled package does **not** trigger a network
install. Trigger installs from a separate, user-initiated action — the
:doc:`tool_registry` page covers that.

Round-trip identity
-------------------

For any input ``data``, ``WorkflowSession(data).to_dict()`` returns a
deep copy of ``data`` byte-for-byte (modulo the clean-form rule for
``enabled``). After a sequence of mutations, the resulting dict reflects
exactly the edits applied — no re-ordering, no re-keyed envelopes. Hosts
can rely on this for diff-driven undo / redo.

Worked example: editor loop
---------------------------

The skeleton of an editor loop mutating, validating, and rendering on
each user action:

.. code-block:: python

   sess = WorkflowSession(initial_data)

   def on_constant_changed(node_name, field, new_value):
       sess.set_constant(node_name, field, new_value)
       errs = sess.validate()
       plan = sess.plan()
       render(sess, errs, plan)

   def on_node_added(node_dict):
       sess.add_node(node_dict)
       render(sess, sess.validate(), sess.plan())

   def on_save():
       Path("workflow.json").write_text(json.dumps(sess.to_dict(), indent=2))

   def on_run():
       wf = sess.to_workflow()
       wf.compute(*targets)
