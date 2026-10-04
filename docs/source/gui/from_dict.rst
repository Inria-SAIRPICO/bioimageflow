Loading: from_dict
==================

:meth:`Workflow.from_dict <bioimageflow.Workflow.from_dict>` materializes a current recursive graph or portable source archive with explicit runtime storage.
The normative grammar, origin and effect boundaries are in :doc:`/reference/unified_workflow_contract`.

Accepted target intent
----------------------

Strict materialization either returns a coherent admitted workflow or refuses before publishing a successful result.
Diagnostic editor materialization may retain unavailable or incomplete nodes with current scoped errors; an incomplete workflow is never represented as executable success.
This is accepted S intent; actual ``2cd79a2`` behavior and the current flags' conformance remain T/C obligations.
The essential distinction is strict use versus diagnostic editing, not every existing flag combination or variable return shape.

Admission checks include required fields, node/edge/port identities, binding kinds and exact source/package identity.
Custom archives admit their complete captured source table before custom-source writes or imports.
Outer ZIP paths and extraction destinations have a separate ownership boundary.
Embedded Python remains trusted executable code; contained staging is not a sandbox.

Current public diagnostic example
----------------------------------

The existing public API exposes ``validate_only`` and ``partial`` flags:

.. code-block:: python

   from bioimageflow import Workflow

   wf, errors = Workflow.from_dict(
       data,
       storage_path=workflow_directory / "results",
       validate_only=True,
       partial=True,
       auto_install=False,
   )

   for error in errors:
       print(error.kind, error.path, error.node, error.field, error.edge_id)

``auto_install=False`` keeps editor/validation paths from implicitly installing missing packages.
Package installation is a separate explicit operation; absent tools produce honest unavailable-tool diagnostics.
Build diagnostics, failed nodes and validation results identify their actual phase and are refreshed when a correcting edit changes the relevant facts.
A later validation must not keep stale build errors as current failures.

Runtime configuration
----------------------

``storage_path`` is required runtime state for records, provenance, run views and owned outputs; it never enters the graph or archive.
Loading a definition is distinct from selecting a live execution engine, manager, cancellation context or callback.
An execution captures its effective inputs, targets and per-environment configuration before effects; later edits apply to later calls.
``Workflow.load(path, storage_path=...)`` is the strict file-oriented entry point.
Current schema version, diagnostic flags and viewer/Core DTO versions describe different contracts and must not be substituted by analogy.
