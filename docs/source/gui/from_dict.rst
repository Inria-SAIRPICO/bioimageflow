Loading: from_dict
==================

:meth:`Workflow.from_dict <bioimageflow.Workflow.from_dict>` is the host's
entry point for materializing a workflow from a wire-format dict. Two
flags drive its behaviour:

- ``validate_only`` — drives the **return type**.
- ``partial`` — drives **error suppression / continuation**.

Their combinations give four modes:

.. list-table::
   :header-rows: 1
   :widths: 25 25 50

   * - ``validate_only``
     - ``partial``
     - Behaviour
   * - ``False`` (default)
     - ``False`` (default)
     - **Strict load.** Returns a fully wired ``Workflow``. The first
       error during construction raises immediately. Use for
       ``Workflow.load(path)``-style runs.
   * - ``False``
     - ``True``
     - **Best-effort load.** Per-node failures are captured. After all
       nodes are processed, the aggregated errors are raised as a single
       ``ValueError``. Rarely the right choice; prefer one of the
       ``validate_only=True`` modes.
   * - ``True``
     - ``False``
     - **Fail-fast diagnostic.** Returns ``(workflow, errors)``; the
       errors list contains at most one entry — the first construction
       failure. Useful for "is this graph loadable?" checks.
   * - ``True``
     - ``True``
     - **Editor mode.** Returns ``(workflow, errors)``; *every*
       per-node failure is captured. The returned workflow may be
       partial — some nodes may be missing or replaced with stubs —
       and ``wf.is_partial`` is ``True``. This is the mode
       :class:`~bioimageflow.WorkflowSession` uses internally.

Inspecting build-time state
---------------------------

After a ``partial`` or ``validate_only`` load, three properties on the
returned ``Workflow`` expose what happened:

- ``wf.errors`` — the full list of :class:`ValidationError` accumulated
  during construction. Same content as the ``errors`` element of the
  returned tuple in ``validate_only=True`` modes; useful when the host
  drops the tuple and just keeps the workflow.
- ``wf.failed_nodes`` — ``dict[node_name, ValidationError]`` mapping
  each node that could not be constructed (e.g. an unknown tool class)
  to the error that prevented it.
- ``wf.is_partial`` — convenience boolean: ``True`` whenever
  ``failed_nodes`` is non-empty.

These properties are populated during ``from_dict`` and persist on the
workflow afterwards, so a later ``wf.validate()`` call does **not**
clobber them.

Other parameters
----------------

- ``auto_install`` (default ``True``) — when ``True``, missing versioned
  packages are installed automatically. **Set to** ``False`` **on hot
  paths** (sessions, validators) to keep keystroke latency low; an
  unknown tool then surfaces as an ``unknown_tool`` error rather than a
  network round-trip.
- ``storage_path_override`` — overrides ``data["config"]["storage_path"]``
  without mutating the dict. Useful when validating a graph against a
  specific cache path (e.g., a sandboxed scratch directory).
- ``on_progress`` / ``engine`` / ``execution`` / ``wetlands_config`` — passed
  through to the constructed :class:`Workflow`. ``None`` means "use the
  values from ``data['config']`` (or defaults)".

``Workflow.load(path)`` is a thin wrapper around the strict mode
(``partial=False, validate_only=False``).

Worked example: GUI loading a stale graph
-----------------------------------------

A user opens a workflow file written months ago. The package
``my_tools`` was renamed to ``my_lab_tools``; one node references the
old name. The host wants to surface the failure inline rather than
crash:

.. code-block:: python

   from bioimageflow import Workflow

   wf, errors = Workflow.from_dict(
       data,
       validate_only=True,
       partial=True,
       auto_install=False,        # don't try to install on the hot path
   )

   if wf.is_partial:
       for name, err in wf.failed_nodes.items():
           print(f"{name}: {err.kind}: {err.message}")

   for e in errors:
       # render with .edge_id for arrow highlighting, .field for the
       # input pin, .path for sub-workflow scoping
       ...

The workflow returned has every loadable node wired correctly; the
broken node is recorded in ``failed_nodes`` and skipped from execution.
Editing the dict to point at the new package name and re-loading
produces a clean workflow.
