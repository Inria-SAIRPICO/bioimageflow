bioimageflow_core
=================

The worker-safe core package. Installed in all environments (main process and
workers). It declares NumPy for shared-memory array helpers and avoids
orchestrator-only dependencies such as pandas and pydantic.
Core supports scientific worker Python >=3.9; the orchestrator floor is >=3.10.
The declaration and current typed-value contracts are defined in :doc:`/specs`; generated member documentation is navigation, not a claim that every internal helper is supported.
Public exported symbols and explicitly documented APIs form the curated support boundary.

Types
-----

.. automodule:: bioimageflow_core.types
   :members:
   :undoc-members:
   :show-inheritance:

Environment
-----------

.. automodule:: bioimageflow_core.environment
   :members:
   :undoc-members:
   :show-inheritance:

Current processing contracts
----------------------------

The public logical DTOs are ``ProcessingTask``, ``RowInvocation``, ``ConsumedRow``, ``OutputGroup`` and ``ProcessingTaskResult``.
Task/result v3 codecs preserve one current typed grammar and exact correlation, including row consumption and the ordered consumed-row identities;
decode is pure and cannot attach, allocate or register owners.
Physical ``mode`` chooses row-chunk or batch execution independently of mapped or collective input meaning.
Mapped results have one singleton-consumption group per input and retain zero/one/many output expansion.
Collective results have exactly one group containing every consumed input identity; an empty batch has an empty consumed tuple and may still produce outputs.
Batch constants/output paths are available as ``ExecutionContext.batch_arguments``; genuine auxiliary selected records use ``reference_rows`` and never become synthetic observation rows.
``ReferenceRow.arguments`` is a dictionary of current typed values.
Portable IOModel annotations are distinct from runtime numeric arrays/scalars, Paths, bytes, literal dictionaries and scoped references.
See :doc:`/specs` §5.2 for the grammar and refusal rules; no historical DTO aliases or wire fallbacks are required.

Supported external-command helpers
-----------------------------------

The supported helper contract owns explicit argv/cwd, staged output, timeout and error boundaries without global chdir or silent partial-output success.
Timeout/interruption cannot certify physical termination without the owning drain fence.
Exact public helper signatures and conformance remain mapped by the ordered test/code review; this reference does not invent an additional method.

Viewer Requirements
-------------------

.. automodule:: bioimageflow_core.viewer
   :members:
   :undoc-members:
   :show-inheritance:

Definition values
-----------------

``IOModel.capture_defaults()`` returns detached declared defaults with missing fields distinct from explicit None.
``bioimageflow_core.defaults.snapshot_value`` detaches supported semantic containers/numeric values without cloning scoped resource owners.
Constructor-supplied values retain caller identity; omitted mutable defaults do not share declaration storage.

Tool Base Classes
-----------------

.. automodule:: bioimageflow_core.tool
   :members:
   :undoc-members:
   :show-inheritance:

Arguments
---------

.. automodule:: bioimageflow_core.arguments
   :members:
   :undoc-members:
   :show-inheritance:

I/O
---

.. automodule:: bioimageflow_core.io
   :members:
   :undoc-members:
   :show-inheritance:

Shared Memory
-------------

.. automodule:: bioimageflow_core.shm
   :members:
   :undoc-members:
   :show-inheritance:

Shared-array ownership
----------------------

.. automodule:: bioimageflow_core.shared_memory
   :members: SharedMemoryContext, CleanupStatus, WorkerGrant, collect_input_scopes, validate_scope_descriptor
