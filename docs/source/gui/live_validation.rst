Live Validation
===============

Three entry points cover the host's needs at three different
granularities — whole graph, single node, single field. Picking the
right one keeps keystroke latency low.

Workflow.validate()
-------------------

:meth:`Workflow.validate <bioimageflow.Workflow.validate>` runs **five
checks**, in order, on a fully-constructed workflow:

1. Cycle detection (one error per cycle).
2. Type compatibility on every column binding.
3. Missing-required-input check for every node.
4. Pydantic validation of every node's supplied constants.
5. Recursive validation of workflow nodes; ``ValidationError.path`` is
   prefixed with the parent's node name.

The method is **non-raising** — it always returns
``list[ValidationError]``, deduplicated and sorted by
``(path, node, field, kind)``. Construction-time errors (steps 1–3) are
already enforced by ``Node.__init__`` for graphs built via the
context-manager API, but ``validate()`` re-runs them so a graph built
via :meth:`from_dict` (or mutated in a session) can be re-checked
after the fact. Step 4 (constant Pydantic validation) only runs in
``validate()`` — it is intentionally not performed at construction
time, so a host editing one field at a time does not need every other
field to be valid yet.

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

   wf = Workflow()
   with wf, wf.capture_errors() as errors:
       loaded = files(path="/data")
       seg    = MyTool()(image=loaded["does_not_exist"])    # column_not_found
       merge  = JoinOnColumn()(seg, other, column="missing")  # column_not_found
   # errors: list[ValidationError] with both failures

Nested blocks push their own list; the outer list is restored on exit.
Best-effort node registration: a node that *partially* constructed —
e.g., one binding failed but the others succeeded — is still attached
to the workflow so subsequent edits and validations can address it.

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
