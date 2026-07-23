Parsl execution
===============

BioImageFlow can dispatch ``ProcessingTool`` work through an attached Parsl runtime while retaining workflow planning, ``DataFrameTool`` execution, cache publication, output views, progress ordering, and recursive workflow assembly in the orchestrator.

Install the optional runtime with ``pip install "bioimageflow[parsl]"``.
Importing ``bioimageflow``, validating or serializing a workflow, and calling ``plan()`` do not import Parsl.

Attached engine construction
----------------------------

A Parsl execution requires explicit runtime configuration.
Construct :class:`~bioimageflow.ParslEngine` with exactly one of a ``parsl.Config`` or an existing DataFlowKernel, plus an :class:`~bioimageflow.ExecutorBinding` for every selectable executor label.

The following local example attests one thread executor for a tool environment:

.. code-block:: python

   from importlib.metadata import version

   from parsl import Config
   from parsl.executors.threads import ThreadPoolExecutor

   from bioimageflow import (
       ExecutorBinding,
       ExecutorCapabilities,
       ParslEngine,
       WorkerEnvironmentAttestation,
       WorkerSlotCapacity,
   )
   from bioimageflow.cache import compute_env_hash

   environment = MyProcessingTool.environment
   label = "local-threads"
   binding = ExecutorBinding(
       label=label,
       environments=(
           WorkerEnvironmentAttestation(
               name=environment.name,
               dependency_hash=compute_env_hash(environment.dependencies),
               allow_flexible_versions=environment.allow_flexible_versions,
               core_requirement=f"bioimageflow-core=={version('bioimageflow-core')}",
           ),
       ),
       capabilities=ExecutorCapabilities(
           storage_modes=("shared_fs",),
           tool_origin_modes=(
               "installed_module",
               "versioned_module",
               "shared_module",
               "source_file",
               "archive_module",
           ),
           slot=WorkerSlotCapacity(cpu=1),
       ),
   )
   config = Config(
       executors=[ThreadPoolExecutor(label=label, max_threads=4)],
       retries=0,
   )

   with ParslEngine(
       parsl_config=config,
       executor_bindings={label: binding},
       resource_lifetime="engine",
   ) as engine:
       result = workflow.compute(engine=engine)

The constructor validates local values but does not start a DataFlowKernel or submit work.
Execution compiles and validates the reachable workflow before acquiring an owned DataFlowKernel.
A fully cached execution does not acquire one.

An application that already owns a DataFlowKernel passes ``dfk=dfk`` and ``resource_lifetime="external"``.
The engine then drains only its own futures and never cleans the caller's DataFlowKernel or unrelated tasks.

Workflow factory and injection
------------------------------

``Workflow.create_engine()`` forwards Parsl runtime values when the workflow preference is ``engine="parsl"``:

.. code-block:: python

   engine = workflow.create_engine(
       parsl_config=config,
       executor_bindings={label: binding},
       resource_lifetime="execution",
   )
   result = workflow.compute(engine=engine)

Passing an explicit engine to ``compute()`` or ``compute_steps()`` overrides the stored workflow engine preference.
A bare ``Workflow(engine="parsl").compute()`` fails with an attached-engine construction error because a workflow file cannot carry live Config, executor, provider, credential, or DataFlowKernel objects.

Bindings, routes, and preflight
-------------------------------

Each executor binding declares:

- the exact Parsl executor label;
- the environment identities already installed on its workers;
- supported storage and tool-origin modes;
- homogeneous CPU, GPU, memory, and GPU-memory capacity for one worker slot.

BioImageFlow never installs packages or starts Wetlands from a Parsl task.
Every reachable ``ProcessingTool`` environment must match an attestation, and every :class:`~bioimageflow_core.ResourceSpec` must fit the selected slot.

Routing uses this deterministic order:

1. ``node_routes`` keyed by the complete scoped node name;
2. ``environment_routes`` keyed by the canonical environment identity;
3. the single compatible binding, when exactly one exists.

Missing, ambiguous, unknown, or incompatible routes fail before processing submission.
Each selected executor label then runs a preflight app that proves executor placement, BioImageFlow worker API and core requirements, shared-root access, sentinel write/read/delete capability, readable dependency paths, and every required tool origin.

Shared storage and tool origins
-------------------------------

``storage_mode="shared_fs"`` is the supported transport.
The workflow storage root, attempt and transient directories, source paths, and optional ``shared_runtime_root`` must resolve identically and be accessible where required by the selected workers.
``storage_mode="staged"`` is rejected because no staging contract is defined.

Installed packages, versioned tool modules, shared project modules, verified source files, and materialized archive modules are supported worker origins.
Archive sources require an absolute ``shared_runtime_root`` and are installed atomically after content verification.

Scheduling and task bounds
--------------------------

``execution="workflow"`` follows the workflow's scheduling policy.
``execution="parallel"`` permits independent processing nodes to overlap.
``execution="sequential"`` permits one node and one unfinished row task at a time.
``DataFrameTool`` and recursive workflow boundaries always execute in the orchestrator.

:class:`~bioimageflow.ParslTaskPolicy` controls row packing and submission pressure:

.. code-block:: python

   from bioimageflow import ParslTaskPolicy

   policy = ParslTaskPolicy(row_chunk_size=8, max_in_flight=64)

``row_chunk_size`` creates consecutive row chunks.
``max_in_flight`` bounds unfinished Parsl futures, and ``ResourceSpec.max_concurrent`` can lower that bound for one processing node.
Whole-node ``process_batch()`` remains one task and emits no row-complete events.

Parsl Config must use ``retries=0``.
BioImageFlow owns deterministic failure selection and therefore rejects opaque scheduler retries.

Lifecycle, cancellation, and errors
-----------------------------------

The ``resource_lifetime`` values are:

- ``"execution"``: create and clean one DataFlowKernel for each execution;
- ``"engine"``: retain an owned DataFlowKernel across executions and clean it on ``close()``;
- ``"external"``: use an injected caller-owned DataFlowKernel.

``close()`` and context-manager exit are idempotent.
An engine rejects overlapping executions and cannot be reused after close.
Closing a stepped iterator or the engine requests cancellation for submitted engine-owned futures, drains all submitted futures, and only then applies resource cleanup.

Call ``workflow.cancel()`` from another thread to cancel the active execution.
Cancellation stops new submission, requests cancellation of outstanding futures, ignores late results, drains writers, and leaves incomplete attempts unselected.
The active cancellation context is cleared when execution finishes, so a later compute does not inherit the request.

Remote failures raise :class:`~bioimageflow.ParslTaskError` with the node, task, executor, attempt, row-position, retry, exception-type, message, and remote traceback identity.
When several tasks fail, the primary exception is selected by compiled workflow and task position rather than completion timing.

Progress and diagnostics
------------------------

Parsl uses the same public ``ProgressEvent`` statuses as the other engines.
Row-complete callbacks are serialized and emitted in aligned row order even when futures complete out of order.
Independent node events may interleave.

Run and node provenance use the effective engine name ``parsl:parallel`` or ``parsl:sequential``.
Backend task diagnostics are stored separately from immutable cache records at:

.. code-block:: text

   diagnostics/v1/runs/<run-id>/nodes/<node-key>/<invocation-id>/tasks/<task-id>.json

A diagnostic records task correlation, executor label, mode, retry, row positions, tool origin, status, timestamps, and terminal error type.
It becomes terminal only after BioImageFlow observes the future.
Attempt and task diagnostics do not contribute to result keys or record IDs.

API
---

.. currentmodule:: bioimageflow

.. autoclass:: ParslEngine
   :members:

.. autoclass:: ParslTaskPolicy
   :members:

.. autoclass:: WorkerSlotCapacity
   :members:

.. autoclass:: ExecutorCapabilities
   :members:

.. autoclass:: WorkerEnvironmentAttestation
   :members:

.. autoclass:: ExecutorBinding
   :members:

.. autoclass:: ParslTaskError
   :members:
