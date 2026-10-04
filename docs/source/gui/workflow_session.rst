WorkflowSession
===============

:class:`~bioimageflow.WorkflowSession` is the dict-backed editor for hosts that mutate a workflow incrementally.
Its canonical definition is editable state; a materialized :class:`~bioimageflow.Workflow` is a cached projection.
Host persistence, revision protocols and draft ownership remain host responsibilities.

Accepted target intent
----------------------

The :doc:`/reference/unified_workflow_contract` defines accepted S intent; source conformance remains for T/C.
Ordinary edits publish one coherent change or leave prior graph, interfaces, edges and name reservations unchanged.
Diagnostic editing can retain incomplete values and current scoped errors without representing them as executable success.
A materialized projection must agree with canonical state after each edit; caller mutation must not silently create a second authority.
Deleting a node removes or explicitly invalidates every dependent edge/interface target/output.
Replacing a binding removes its old data dependency while preserving genuine detached completion obligations.

Construction and edits
-----------------------

Runtime storage is supplied separately and never enters a graph snapshot:

.. code-block:: python

   from bioimageflow import WorkflowSession

   sess = WorkflowSession(data, storage_path=workflow_directory / "results")
   sess.set_constant("threshold", "cutoff", 100.0)
   sess.set_enabled("filter", False)
   errors = sess.validate()
   plan = sess.plan()
   snapshot = sess.to_dict()

The public edit surface includes ``add_node``, ``remove_node``, ``add_edge``, ``remove_edge``, ``set_constant`` and ``set_enabled``.
Node records use the current tool/workflow variant and exact executable identity; edges use an explicit column/DataFrame variant and stable ID.
``set_constant`` stores a faithfully supported constant envelope or refuses; it never silently converts unsupported data into strings.
Accepted constant/default/nullability rules are independent of GUI widget hints.
Re-enabling omits the default ``enabled`` field without erasing wiring.

Freshness and caching
----------------------

An edit invalidates affected schema, validation and planning facts according to its meaning, not only a structural/non-structural label.
Corrected errors must not survive as current diagnostics merely because a materialized object was reused.
Unchanged tool identity need not be re-imported for every parameter or enabled-state edit; in-place mutation is an optimization choice rather than the normative ownership contract.
``plan()`` refreshes storage-facing selection state on each call; it is a diagnostic snapshot, not a guarantee that later execution can reuse it unchanged.
Read-only views and ``to_dict()`` return detached snapshots with stable identities and faithful current values.
The target is semantic current-grammar fidelity, not byte-for-byte preservation of historical formats or arbitrary invalid dictionaries.

Materialize and run
--------------------

Current ``to_workflow()`` uses diagnostic ``Workflow.from_dict`` materialization with installation disabled; ``errors``, ``failed_nodes`` and ``is_partial`` must be interpreted honestly.
A host renders unavailable tools or incomplete bindings inline and chooses an explicit install/correction operation.
Before execution, it obtains a coherent admitted definition and supplies root inputs/configuration as required.
One run captures an immutable effective definition; edits made during that run affect later executions.
The accepted intent does not claim that the current source already enforces every snapshot or rejection boundary.
