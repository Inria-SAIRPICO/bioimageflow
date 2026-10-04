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

The public logical DTOs are ``ProcessingTask``, ``RowInvocation``, ``ProcessingTaskResult`` and ``RowResult``.
Task/result codecs preserve one current typed grammar and exact correlation; decode is pure and cannot attach, allocate or register owners.
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
