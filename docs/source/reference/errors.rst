Errors
======

This page is the canonical catalogue of validation errors and domain
exceptions raised by BioImageFlow. The full contract lives in
:doc:`/specs` §6.6; this reference is the curated, GUI-oriented
view.

ValidationError
---------------

:class:`~bioimageflow.validation.ValidationError` is the dataclass that
represents a single problem found during graph construction or
validation. The library never raises ``ValidationError`` itself — it
either raises one of the domain exceptions (see below) or appends a
``ValidationError`` to an active error collector.

.. list-table::
   :header-rows: 1
   :widths: 18 82

   * - Field
     - Description
   * - ``kind``
     - One of the values in :class:`ValidationErrorKind` (see below).
       Stable string; primary key for routing in GUIs.
   * - ``message``
     - Human-readable, English. Free-form; not stable across versions.
   * - ``node``
     - Scoped name of the node the error applies to, or ``None`` for
       graph-level errors (cycles, construction failures).
   * - ``field``
     - Name of the input field involved, or ``None`` when the error is
       not field-specific.
   * - ``edge``
     - ``(from_node, to_node, field)`` triple for edge errors;
       ``None`` otherwise.
   * - ``edge_id``
     - Optional opaque GUI-supplied id for the edge (round-tripped
       through :meth:`Workflow.to_dict` / :meth:`from_dict`). The
       primary disambiguator for positional edges and the field a host
       matches when highlighting an arrow.
   * - ``path``
     - Tuple of parent node names for sub-workflow scoping. Empty
       tuple ``()`` for top-level errors;
       ``("outer",)`` for errors inside a sub-workflow ``outer``;
       ``("outer", "inner")`` for errors nested two levels deep.

ValidationErrorKind
-------------------

The full set of kinds, drawn from :data:`bioimageflow.validation.ValidationErrorKind`:

.. list-table::
   :header-rows: 1
   :widths: 25 35 40

   * - Kind
     - When raised
     - Populated fields
   * - ``cycle``
     - The graph contains a directed cycle (specs.md §6.5).
     - ``message``
   * - ``type_mismatch``
     - A column binding's producer ``ImageSpec`` is incompatible with
       the consumer's input ``ImageSpec``.
     - ``node``, ``field``, ``edge``, ``edge_id``
   * - ``missing_input``
     - A required input has neither a column binding, a constant, nor
       a default.
     - ``node``, ``field``
   * - ``unknown_input``
     - A binding refers to a field that is not declared on the tool's
       ``Inputs``.
     - ``node``, ``field``
   * - ``column_not_found``
     - A ``ColumnRef`` references a column that does not exist in the
       upstream node's resolved output schema.
     - ``node``, ``field``, ``edge``, ``edge_id``
   * - ``parameter_invalid``
     - Pydantic validation failed for a constant value (range,
       conversion, ...). Reported by ``Workflow.validate()`` and
       :func:`~bioimageflow.validation.validate_parameters`.
     - ``node``, ``field``
   * - ``unknown_tool``
     - A node's tool class could not be resolved during ``from_dict``
       (package not installed, class not found, ``auto_install=False``).
     - ``node``
   * - ``duplicate_name``
     - Two nodes share the same name within a workflow.
     - ``node``
   * - ``construction_failed``
     - Graph construction raised in
       ``Workflow.from_dict(validate_only=True)`` mode. The wrapped
       exception's text is captured in ``message``.
     - ``message``
   * - ``source_tool_upstream``
     - A source tool (``accepts_upstream = False``) was given a
       positional upstream argument.
     - ``node``

Domain exceptions
-----------------

These exceptions are raised by the library outside an error-collector
context. Each carries a ``to_validation_error(...)`` method that maps
it onto the matching :class:`ValidationError` for GUIs that catch and
surface them:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Exception
     - When raised
   * - :class:`~bioimageflow.BindingError`
     - A required input has no source (no column ref, no constant, no
       default) at construction time. Maps to ``missing_input``.
   * - :class:`~bioimageflow.ColumnNotFoundError`
     - ``node["col"]`` references a column not in the node's output
       schema. Maps to ``column_not_found``.
   * - :class:`~bioimageflow.IndexAlignmentError`
     - Multiple upstream DataFrames cannot be aligned by index at
       execution time (e.g., row counts diverge in a ProcessingTool
       merge). Maps to ``construction_failed``.
   * - :class:`~bioimageflow.SourceToolUpstreamError`
     - A source ``DataFrameTool`` was constructed with positional
       upstream arguments. Maps to ``source_tool_upstream``.
   * - :class:`~bioimageflow.engine.DisabledNodeError`
     - ``compute()`` was called with all targets skipped, or
       ``NodeStep.execute()`` was called on a skipped step.
   * - :class:`~bioimageflow.engine.CycleInWorkflowError`
     - ``Workflow.plan()`` was called on a cyclic graph.
       ``Workflow.validate()`` reports the same condition as a
       ``cycle`` :class:`ValidationError`.
   * - :class:`~bioimageflow.engine.WorkflowCancelledError`
     - ``Workflow.cancel()`` was observed by the engine during
       ``compute()``.
   * - :class:`~bioimageflow_core.environment.EnvironmentMismatchError`
     - Two ``EnvironmentSpec`` instances share a name but differ in
       declared dependencies.

Cycle handling
--------------

Cycles are reported in two different ways:

- :meth:`Workflow.validate` returns a ``cycle`` ``ValidationError`` and
  continues — other steps still run, so a graph with a cycle plus
  parameter errors gets all errors at once.
- :meth:`Workflow.plan` raises :class:`CycleInWorkflowError` because a
  cyclic graph cannot be topologically ordered for planning.

Hosts that drive both should call ``validate()`` first and gate
``plan()`` on no ``cycle`` entries — see :doc:`/gui/planning_and_cache`.
