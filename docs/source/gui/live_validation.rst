Live Validation
===============

Accepted S intent distinguishes declaration, partial editor diagnostics and resolved execution admission.
Actual source conformance remains a T/C obligation; see :doc:`/reference/unified_workflow_contract`.

Three entry points cover the host's needs at three different
granularities — whole graph, single node, single field. Picking the
right one keeps keystroke latency low.

Workflow.validate()
-------------------

:meth:`Workflow.validate <bioimageflow.Workflow.validate>` reports current definition and binding diagnostics without executing scientific tools or implicitly installing heavy dependencies.
Its responsibilities include:

1. Cycle detection (one error per cycle).
2. Type compatibility on every column binding.
3. Missing-required-input check for every node.
4. Pydantic validation of every node's supplied constants.
5. Recursive validation of workflow nodes; ``ValidationError.path`` is
   prefixed with the parent's node name.

Ordinary graph diagnostics return structured errors with scoped node/field/edge identity.
Construction admission, editable incomplete values and resolved runtime validation are separate phases; a list of graph diagnostics is not execution certification.
Unknown schema stays explicitly unknown; declarations, GUI hints and scientific value constraints are not interchangeable.
Only declared meaning-preserving conversions are admitted; unsupported scientific constants are not silently stringified.
Correcting an edit refreshes affected errors and schemas.
Owner interruption and setup failures must not be disguised as an empty successful diagnostic list.

.. code-block:: python

   errors = wf.validate()
   for err in errors:
       print(err.kind, err.path, err.node, err.field, err.message)

Workflow.capture_errors()
-------------------------

:meth:`Workflow.capture_errors <bioimageflow.Workflow.capture_errors>`
is a context manager that flips ``Node.__init__`` from "raise on first
error" to "append to this list". Use it when **building** the graph
programmatically and the host wants every error from a single
construction pass:

.. code-block:: python

   wf = Workflow(storage_path="./results")
   with wf, wf.capture_errors() as errors:
       loaded = files(path="/data")
       seg    = MyTool()(image=loaded["does_not_exist"])    # column_not_found
       merge  = JoinOnColumn()(seg, other, column="missing")  # column_not_found
   # errors: list[ValidationError] with both failures

Nested blocks push their own list; the outer list is restored on exit.
Diagnostic mode can deliberately retain an incomplete node and scoped errors so later editing can address it.
This differs from ordinary mutation refusal, whose accepted target is all-or-nothing publication of graph/interface/edge/name state.
An incomplete diagnostic node is never advertised as executable success.

The relationship to ``validate()``:

- ``capture_errors()`` is for "I'm building the graph".
- ``validate()`` is for "the graph is built; re-check it".

Single-node and single-field helpers
------------------------------------

Two pure functions in :mod:`bioimageflow.validation` cover the cases
where running the whole pipeline is overkill.

validate_parameters
~~~~~~~~~~~~~~~~~~~

:func:`~bioimageflow.validation.validate_parameters` validates a dict
of constants against a tool class's ``Inputs`` without constructing a
workflow:

.. code-block:: python

   from bioimageflow.validation import validate_parameters

   errors = validate_parameters(
       MyTool,
       {"sigma": 1.5, "iterations": -3},
       node="filter",
   )
   # [ValidationError(kind="parameter_invalid", node="filter",
   #                  field="iterations", message="Input should be greater than 0")]

Only the **supplied** parameters are checked; missing required fields
are reported by ``Workflow.validate`` (kind ``missing_input``), not
here. This is the function inline form widgets call when a user types
in a number field — fast, no workflow needed.

check_type_compat
~~~~~~~~~~~~~~~~~

:func:`~bioimageflow.validation.check_type_compat` checks whether a
single column reference is compatible with a single input field:

.. code-block:: python

   from bioimageflow.validation import check_type_compat

   err = check_type_compat(node, field="image", col_ref=upstream["mask"])
   # None on success; ValidationError(kind="type_mismatch", ...) on failure

This is what an editor calls when the user is dragging an edge in
progress: hover over a target pin, run ``check_type_compat`` against
the candidate column, light the pin green or red. No workflow is
mutated.

Picking the right entry point
-----------------------------

A short decision table:

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Situation
     - Use
   * - Building the graph programmatically; want every error
     - ``Workflow.capture_errors()``
   * - Graph already built; re-check after edits or load
     - ``Workflow.validate()``
   * - User typed in a numeric / string field
     - ``validate_parameters(tool_class, params)``
   * - User is dragging an edge in progress
     - ``check_type_compat(node, field, col_ref)``

The full ``ValidationErrorKind`` table — every value, when it is
raised, what fields it populates — lives under the reference tree.
