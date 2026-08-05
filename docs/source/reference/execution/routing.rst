Parsl Workers and Routing
=========================

This page explains how BioImageFlow chooses the Parsl worker pool that will run each ProcessingTool node.
You only need routing when using Parsl.
Normal local execution chooses local environment workers automatically.

The basic idea
--------------

A Parsl **executor** is a named destination for tasks.
Depending on its configuration, an executor may represent four local threads, CPU workers in a Slurm partition, GPU workers in another partition, or another pool of machines.

Consider a workflow with these nodes:

- ``normalize`` needs two CPU cores;
- ``segment`` needs one GPU and 16 GB of GPU memory.

And a Parsl configuration with these executors:

- ``cpu-workers`` offers CPU-only worker slots;
- ``gpu-workers`` offers one GPU and 24 GB of GPU memory per slot.

``normalize`` can run on either pool if both have its software environment.
``segment`` can run only on ``gpu-workers`` because the CPU pool has no GPU.
Choosing a suitable executor for a node is called **routing**.

How compatibility is decided
----------------------------

BioImageFlow does not guess what an executor provides from a label such as ``gpu-workers``.
The person or application configuring Parsl supplies an :class:`~bioimageflow.ExecutorBinding` that describes the executor.

BioImageFlow calls an executor **compatible** with a node when all of the following are true:

- the worker has the tool's declared software environment;
- one worker slot has enough CPU, GPU, system memory, and GPU memory;
- the worker can access the workflow's storage;
- the worker can load the tool's Python code.

If an executor does not satisfy all four conditions, BioImageFlow will not send that node to it.
If several compatible executors remain, the configuration must choose one with a route.

The static planning operation and real execution use the same compatibility and route-selection functions.
In practical terms, a GUI or command-line preflight cannot say “this node will use the GPU pool” using one set of rules and then have runtime silently choose differently using another set.

Three related Parsl terms
-------------------------

**Executor**
   Accepts individual Python tasks from the BioImageFlow orchestrator and sends them to its workers.
   Every executor has a label such as ``cpu-workers``.

**Provider**
   Obtains machines for an executor.
   For example, a Parsl Slurm provider submits worker jobs to Slurm.
   A thread executor running on the current machine does not need a cluster provider.

**Executor binding**
   BioImageFlow's description of what one named executor can safely run.
   It does not create workers; the Parsl Config does that.

Why bindings are explicit
-------------------------

Parsl knows how to submit a task to an executor, but it does not know BioImageFlow's tool-environment identity, tool origin, storage rules, or portable resource contract.
The binding supplies this missing information in a serializable and inspectable form.

Each :class:`~bioimageflow.ExecutorBinding` contains:

- ``label``: the exact label used by the executor in the Parsl Config;
- ``environments``: software environments known to be installed on its workers;
- ``capabilities.storage_modes``: how workers access workflow data;
- ``capabilities.tool_origin_modes``: which forms of tool code workers can load;
- ``capabilities.slot``: the resources guaranteed to one task slot.

The key in the ``executor_bindings`` dictionary must equal ``binding.label``.
The resolved Parsl Config and binding dictionary must describe the same set of executor labels.
This catches spelling mistakes and stale configuration before workflow tasks are submitted.

Describe a worker environment
-----------------------------

Parsl workers do not use BioImageFlow's local environment provisioning.
The cluster or Parsl configuration must make the required packages available before the worker starts.

A :class:`~bioimageflow.WorkerEnvironmentAttestation` is a declaration that a particular environment is present on an executor's workers:

.. code-block:: python

   from importlib.metadata import version

   from bioimageflow import WorkerEnvironmentAttestation
   from bioimageflow.cache import compute_env_hash

   environment = segment.environment
   torch_environment = WorkerEnvironmentAttestation(
       name=environment.name,
       dependency_hash=compute_env_hash(environment.dependencies),
       allow_flexible_versions=environment.allow_flexible_versions,
       core_requirement=f"bioimageflow-core=={version('bioimageflow-core')}",
   )

“Attestation” means that the configuration claims these packages are already available.
It is not blind trust: before processing starts, runtime preflight asks a real worker to verify its BioImageFlow core, shared paths, and required tool code.

An administrator commonly builds these values as part of a reusable cluster configuration.
A workflow user should normally select that configuration rather than calculating dependency hashes manually.

Describe one worker slot
------------------------

A worker slot is the capacity available to one task at a time.
This binding describes a GPU executor:

.. code-block:: python

   from bioimageflow import (
       ExecutorBinding,
       ExecutorCapabilities,
       WorkerSlotCapacity,
   )

   gpu_binding = ExecutorBinding(
       label="gpu-workers",
       environments=(torch_environment,),
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

The values describe a single task slot, not the total size of the cluster allocation.
A node requesting four CPUs and one GPU fits this slot; a node requesting two GPUs does not.

``max_concurrent`` is different from slot capacity.
It limits how many unfinished tasks one workflow node may have, while the executor and provider decide how many slots exist in total.

Automatic and explicit routes
-----------------------------

If exactly one executor is compatible with a node, BioImageFlow selects it automatically.
If several executors are compatible, make the choice explicit.

A node route chooses an executor for one exact scoped node path:

.. code-block:: python

   node_routes = {
       "analysis/large-segmentation": "gpu-workers",
   }

An environment route chooses an executor for every node using one stable environment identity:

.. code-block:: python

   environment_routes = {
       torch_environment_identity: "gpu-workers",
   }

The scoped node path includes nested workflow names, which is why the example contains ``analysis/``.
Static planning returns both ``scoped_node_path`` and ``environment_identity`` so applications do not need to reconstruct either value.

BioImageFlow applies route choices in this order:

1. an exact node route;
2. an environment route;
3. the only compatible executor, when there is exactly one.

An unknown label, an explicit route to an incompatible executor, no compatible executor, or several compatible executors without a route is reported before processing starts.

Storage compatibility
---------------------

The supported storage mode is ``shared_fs``.
It means that the orchestrator and workers see the same workflow storage at the same absolute path.
For example, ``/cluster/project/workflow/results`` must refer to the same directory from both the orchestrator job and worker jobs.

BioImageFlow currently rejects ``storage_mode="staged"``.
There is no contract for copying each task's inputs and outputs independently between an orchestrator and workers that do not share storage.

Tool-code compatibility
-----------------------

Workers also need the Python code that implements each ProcessingTool.
Bindings state which supported origins are available:

- ``installed_module``: a normally installed Python package;
- ``versioned_module``: a package loaded from BioImageFlow's versioned tool store;
- ``shared_module``: a project module on shared storage;
- ``source_file``: a verified Python source file;
- ``archive_module``: code materialized from a workflow archive.

Archive code needs an absolute ``shared_runtime_root`` visible to orchestrator and workers.
BioImageFlow verifies its content and installs it atomically before tasks use it.

Preview routing without starting workers
----------------------------------------

:func:`~bioimageflow.plan_distributed_execution` answers “where could each node run?” without starting Parsl:

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
       print(node.scoped_node_path)
       print("  needs:", node.resources)
       print("  compatible:", node.compatible_executors)
       print("  selected:", node.selected_executor)
       print("  reason:", node.route_reason)
       print("  rejected because:", node.incompatibilities)

The plan also reports cached nodes that will not be dispatched.
Its values can cross a process boundary with ``to_dict()`` and ``from_dict()``.

Planning does not import Parsl, start Parsl's in-process coordinator (the DataFlowKernel), create workers, submit a scheduler job, or create a workflow run.
It compares declarations only.

Verify real workers before processing
-------------------------------------

Static planning cannot prove that a future worker machine is correctly installed or that shared storage is functioning.
After workers exist, but before BioImageFlow sends processing tasks, runtime preflight checks each selected executor.

The check confirms that:

- the test task arrived on the expected executor;
- the worker has a compatible BioImageFlow core;
- shared directories are readable and writable as required;
- declared dependency paths are readable;
- the required tool code can be loaded.

Planning gives an early, non-allocating answer from configuration.
Runtime preflight verifies the actual allocated workers.
