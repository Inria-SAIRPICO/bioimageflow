Attached Parsl Execution
========================

Attached execution keeps the BioImageFlow orchestrator in the caller's Python process and dispatches ProcessingTool tasks through Parsl.
Use it when an application wants immediate ``compute()`` results or already owns a Parsl DataFlowKernel.

Install ``bioimageflow[parsl]`` before using this mode.
Importing BioImageFlow, serializing workflows, inspecting capabilities, and static distributed planning do not require or import Parsl.

Required configuration
----------------------

An attached :class:`~bioimageflow.ParslEngine` needs exactly one of:

- a Parsl ``Config`` that BioImageFlow may use to create a DataFlowKernel; or
- an existing caller-owned DataFlowKernel.

It also needs one public :class:`~bioimageflow.ExecutorBinding` for every Parsl executor label that workflow tasks may use.
A binding attests the environment, storage, tool origins, and resource capacity that actually exist on its workers.

Minimal local example
---------------------

The following example attaches a four-thread executor to a workflow whose ProcessingTool uses ``GENERAL_ENV``:

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
   from bioimageflow_core import GENERAL_ENV

   label = "local-threads"
   binding = ExecutorBinding(
       label=label,
       environments=(
           WorkerEnvironmentAttestation(
               name=GENERAL_ENV.name,
               dependency_hash=compute_env_hash(GENERAL_ENV.dependencies),
               allow_flexible_versions=GENERAL_ENV.allow_flexible_versions,
               core_requirement=(
                   f"bioimageflow-core=={version('bioimageflow-core')}"
               ),
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
       result = workflow.compute(target, engine=engine)

The constructor validates local values but does not create a DataFlowKernel or submit work.
Execution first compiles and validates the reachable graph; a fully cached execution does not acquire a DataFlowKernel.

Parsl must use ``retries=0``.
BioImageFlow owns deterministic task failure and attempt semantics and rejects opaque Parsl retries.

Use a trusted configuration reference
-------------------------------------

An application can construct an attached engine from a public :class:`~bioimageflow.ParslConfigRef`:

.. code-block:: python

   from bioimageflow import ParslConfigRef, ParslEngine

   reference = ParslConfigRef(
       "my_application.parsl_config:build",
       {"profile": "interactive"},
   )
   engine = ParslEngine.from_config_ref(
       reference,
       executor_bindings=bindings,
       trusted_factories={"my_application.parsl_config:build"},
   )

The trusted boundary accepts finite JSON-safe factory arguments and environment-variable secret references.
It rejects unsafe values, secret-looking literal arguments, unexpected executor labels, and nonzero Parsl retries.
Use :func:`~bioimageflow.validate_parsl_config_ref` to obtain sanitized structured diagnostics without creating a DataFlowKernel.

Inject an existing DataFlowKernel
---------------------------------

If another component owns the DataFlowKernel, pass it explicitly:

.. code-block:: python

   with ParslEngine(
       dfk=dfk,
       executor_bindings=bindings,
       resource_lifetime="external",
   ) as engine:
       result = workflow.compute(target, engine=engine)

BioImageFlow drains only futures it submitted and never cleans the caller's DataFlowKernel or unrelated tasks.
An injected DataFlowKernel requires ``resource_lifetime="external"``.

Engine creation from a workflow
-------------------------------

``Workflow.create_engine()`` forwards Parsl runtime values when the workflow preference is ``engine="parsl"``:

.. code-block:: python

   engine = workflow.create_engine(
       parsl_config=config,
       executor_bindings=bindings,
       resource_lifetime="execution",
   )
   result = workflow.compute(target, engine=engine)

Passing an explicit engine to ``compute()`` or ``compute_steps()`` overrides the saved engine preference.
A workflow definition cannot persist a live Config, provider, executor, credential, or DataFlowKernel, so a bare workflow with ``engine="parsl"`` still needs runtime injection.

Task packing and submission pressure
------------------------------------

:class:`~bioimageflow.ParslTaskPolicy` controls row chunks and unfinished futures:

.. code-block:: python

   from bioimageflow import ParslTaskPolicy

   policy = ParslTaskPolicy(
       row_chunk_size=8,
       max_in_flight=64,
   )

``row_chunk_size`` groups consecutive rows into one Parsl task.
``max_in_flight`` bounds unfinished Parsl futures across dispatch, while a node's effective ``max_concurrent`` may impose a lower per-node bound.
A whole-node ``process_batch()`` remains one task.

Lifecycle
---------

The resource lifetime values are:

- ``"execution"``: create and clean an owned DataFlowKernel for each execution;
- ``"engine"``: retain an owned DataFlowKernel across executions and clean it on ``close()``;
- ``"external"``: retain the caller-owned DataFlowKernel untouched.

``close()`` and context-manager exit are idempotent.
An engine rejects overlapping executions and cannot execute after it closes.
Closing a stepped iterator requests cancellation of engine-owned futures, drains submitted work, and then applies the configured resource cleanup.

See :doc:`routing` for binding construction, routes, and preflight, and :doc:`monitoring` for progress, failure, and cancellation semantics.
