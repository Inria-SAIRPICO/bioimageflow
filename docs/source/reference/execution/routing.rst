Parsl Bindings, Planning, and Routing
=====================================

BioImageFlow routes a ProcessingTool node only to an executor whose public binding is compatible with the node.
Planning and runtime dispatch use the same requirement, compatibility, and route-selection implementation.

Executor bindings
-----------------

Each :class:`~bioimageflow.ExecutorBinding` contains:

- the exact Parsl executor label;
- one or more worker environment attestations;
- supported storage modes and tool-origin modes;
- the CPU, GPU, system-memory, and GPU-memory capacity of one homogeneous worker slot.

The dictionary key and ``binding.label`` must match.
Every label exposed by the resolved Parsl Config must have a supplied binding, and every supplied binding must name an actual executor.

Environment attestations
------------------------

A :class:`~bioimageflow.WorkerEnvironmentAttestation` identifies an environment already installed on workers:

.. code-block:: python

   from importlib.metadata import version

   from bioimageflow import WorkerEnvironmentAttestation
   from bioimageflow.cache import compute_env_hash

   environment = processing_tool.environment
   attestation = WorkerEnvironmentAttestation(
       name=environment.name,
       dependency_hash=compute_env_hash(environment.dependencies),
       allow_flexible_versions=environment.allow_flexible_versions,
       core_requirement=f"bioimageflow-core=={version('bioimageflow-core')}",
   )

Parsl workers are not provisioned through the local environment manager.
The profile author is responsible for making the attested dependencies and compatible ``bioimageflow-core`` available on the worker.

Capabilities and slot capacity
------------------------------

.. code-block:: python

   from bioimageflow import (
       ExecutorBinding,
       ExecutorCapabilities,
       WorkerSlotCapacity,
   )

   gpu_binding = ExecutorBinding(
       label="gpu",
       environments=(torch_attestation,),
       capabilities=ExecutorCapabilities(
           storage_modes=("shared_fs",),
           tool_origin_modes=(
               "installed_module",
               "versioned_module",
               "shared_module",
               "source_file",
               "archive_module",
           ),
           slot=WorkerSlotCapacity(
               cpu=8,
               gpu=1,
               memory_bytes=64 * 1024**3,
               gpu_memory_bytes=24 * 1024**3,
           ),
       ),
   )

The effective node requirement must fit within one advertised slot.
``max_concurrent`` is a submission bound and is not slot capacity.

Storage and tool origins
------------------------

``storage_mode="shared_fs"`` is the supported data transport.
The workflow storage root and any shared input, runtime, source, attempt, and transient paths must resolve identically on the orchestrator and relevant workers.
``storage_mode="staged"`` is rejected because BioImageFlow has no task-level staging contract.

Supported tool origins are installed modules, versioned modules, shared project modules, verified source files, and materialized archive modules.
Archive sources require an absolute ``shared_runtime_root`` and are installed atomically after content verification.

Route selection
---------------

For each scoped ProcessingTool node, BioImageFlow considers compatible bindings and selects a label in this order:

1. ``node_routes`` keyed by the complete scoped node path;
2. ``environment_routes`` keyed by canonical environment identity;
3. the sole compatible executor, if exactly one remains.

.. code-block:: python

   engine = ParslEngine(
       parsl_config=config,
       executor_bindings={
           "cpu": cpu_binding,
           "gpu": gpu_binding,
       },
       node_routes={"analysis/large-segmentation": "gpu"},
       environment_routes={torch_environment_key: "gpu"},
       shared_runtime_root="/shared/bioimageflow/runtime",
   )

Unknown labels, incompatible explicit routes, no compatible executor, and ambiguous compatible executors fail before processing submission.
Scoped paths include nested workflow context and are also used by progress and diagnostics.
The static plan exposes each node's ``environment_identity``; an application may use that returned value to build an environment route without reproducing its canonicalization algorithm.

Static planning
---------------

:func:`~bioimageflow.plan_distributed_execution` checks a workflow without starting Parsl:

.. code-block:: python

   from bioimageflow import plan_distributed_execution

   plan = plan_distributed_execution(
       workflow,
       executor_bindings=bindings,
       node_routes=node_routes,
       environment_routes=environment_routes,
       shared_runtime_root="/shared/bioimageflow/runtime",
       storage_mode="shared_fs",
       task_policy=task_policy,
   )

   for node in plan.nodes:
       print(
           node.scoped_node_path,
           node.resources,
           node.compatible_executors,
           node.selected_executor,
           node.route_reason,
           node.incompatibilities,
       )

The returned values are typed and JSON-serializable through ``to_dict()``.
The plan includes environment, tool-origin, storage, capacity, and route incompatibilities for every scoped ProcessingTool node.

This operation is deterministic and non-allocating.
It does not import Parsl, create a DataFlowKernel, allocate a worker, submit a scheduler job, or create a workflow run.

Runtime preflight
-----------------

After a route is selected and before processing work is submitted, attached runtime preflight proves:

- the task actually ran on the selected executor label;
- the worker BioImageFlow API and core requirement are compatible;
- shared roots are accessible and a sentinel can be written, read, and removed;
- declared dependency paths are readable;
- required tool origins are available and verified.

Static planning describes compatibility from declarations.
Runtime preflight verifies that the running executors satisfy those declarations.
