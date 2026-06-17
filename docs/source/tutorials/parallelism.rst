Parallelism
===========

BioImageFlow has two local execution backends.
The default ``engine="direct"`` runs tool code in the orchestrator process and
uses ``execution="parallel"`` to schedule ready independent nodes concurrently.
The ``engine="wetlands"`` backend runs :class:`~bioimageflow_core.ProcessingTool`
methods in isolated worker processes and is the backend that supports row-level
worker-process parallelism. This page covers the knobs that size those modes.

What can run in parallel
------------------------

Two sources of local concurrency are available:

- **Independent DAG branches** run **concurrently**. Two ``ProcessingTool``
  nodes with no unresolved dependencies may be scheduled at the same time
  when ``execution="parallel"`` is used.
- **Rows of a single ProcessingTool** run **in parallel** only with
  ``engine="wetlands"``, across the worker processes of that tool's
  environment pool. Pool sizing is controlled by ``max_workers`` (see below).

DataFrameTools always run in the main process; their
``transform`` / ``merge_dataframes`` calls are not parallelised.

Workflow-level Wetlands baseline
--------------------------------

``Workflow(max_workers=N)`` sets the default Wetlands per-environment worker
count for tools that don't override it:

.. code-block:: python

   from bioimageflow import Workflow

   with Workflow(max_workers=4, engine="wetlands") as wf:
       ...

The default is ``max_workers=1`` — a single worker per Wetlands environment.
The setting is ignored by the direct engine for row dispatch.

Per-environment overrides
-------------------------

:meth:`~bioimageflow.Workflow.get_environment` returns a mutable
:class:`~bioimageflow.WorkflowEnvironment` proxy keyed by
:class:`~bioimageflow_core.EnvironmentSpec` name. Setting
``max_workers`` on the proxy overrides the workflow default for tools in
that environment:

.. code-block:: python

   from bioimageflow_core import GENERAL_ENV
   from bioimageflow_segmentation_tools import Cellpose3

   cellpose = Cellpose3()                    # has its own EnvironmentSpec
   filter_tool = FilterByArea()              # uses GENERAL_ENV

   with Workflow(max_workers=4, engine="wetlands") as wf:
       wf.get_environment(cellpose).max_workers = 1   # GPU tool — keep serial
       wf.get_environment(GENERAL_ENV).max_workers = 8

The proxy can be addressed by ``ProcessingTool`` instance,
``EnvironmentSpec`` instance, or environment name string.

Per-worker environment variables
--------------------------------

``WorkflowEnvironment.worker_env`` is a callable
``(worker_index: int) -> dict[str, str]`` invoked when each worker
process is spawned. Use it to assign GPUs, set thread caps, or pass
worker-specific configuration:

.. code-block:: python

   gpu_tool = MyGPUTool()
   with Workflow(engine="wetlands") as wf:
       env = wf.get_environment(gpu_tool)
       env.max_workers = 4
       env.worker_env = lambda i: {"CUDA_VISIBLE_DEVICES": str(i)}

ResourceSpec
------------

:class:`~bioimageflow_core.ResourceSpec` declares the resources a tool
needs. Engines read it to size pools and inject environment variables:

.. code-block:: python

   from bioimageflow_core import ProcessingTool, EnvironmentSpec, ResourceSpec, Template

   class MyGPUTool(ProcessingTool):
       display_name = "GPU Segmenter"
       environment = EnvironmentSpec(name="torch", dependencies={...})
       resources = ResourceSpec(gpu=1, gpu_memory="8GB")

Fields:

- ``cpu`` — CPU cores per row (default ``1``).
- ``gpu`` — GPUs per row (default ``0``).
- ``gpu_memory`` — string hint, e.g. ``"8GB"``.
- ``max_concurrent`` — upper bound on simultaneous rows (default ``0``,
  meaning no limit beyond ``max_workers``).
- ``memory`` — string hint for system memory.

The Wetlands backend honours ``gpu``: when a tool's ``ResourceSpec`` has
``gpu >= 1`` and no explicit ``worker_env`` is set on its environment,
the backend auto-installs
``worker_env = lambda i: {"CUDA_VISIBLE_DEVICES": str(i)}``. The Parsl
engine is reserved for richer resource scheduling; the contract for
``cpu`` / ``memory`` / ``max_concurrent`` lives with that implementation.

worker_timeout
--------------

``WorkflowEnvironment.worker_timeout`` (seconds) is a last-resort safety
net for native code that deadlocks. The engine wraps each row dispatch
in ``task.wait_for(timeout)`` and raises
:class:`~bioimageflow.engine.WorkerTimeoutError` if the task does not
complete:

.. code-block:: python

   wf.get_environment("torch").worker_timeout = 600.0  # 10 minutes

Leave it ``None`` (the default) when the tool's runtime is bounded; set
it only when you have observed deadlocks in third-party code.

execution="sequential" for debugging
------------------------------------

Constructing a workflow with ``execution="sequential"`` and ``max_workers=1``
gives deterministic, single-threaded execution that is easier to debug:

.. code-block:: python

   with Workflow(execution="sequential", max_workers=1) as wf:
       ...

Use it when chasing a non-deterministic bug; switch back to the default
once the bug is reproduced.

Worked example: a GPU tool with row-level parallelism
-----------------------------------------------------

A tool declares ``ResourceSpec(gpu=1)`` and lives in its own environment.
With ``engine="wetlands"``, the workflow runs four rows of it in parallel,
each pinned to a distinct GPU:

.. code-block:: python

   from bioimageflow_core import ProcessingTool, EnvironmentSpec, ResourceSpec

   class Segment(ProcessingTool):
       display_name = "Segment"
       environment = EnvironmentSpec(name="torch", dependencies={...})
       resources = ResourceSpec(gpu=1)

       class Inputs:
           image: Annotated[Path, ImageSpec()]

       class Outputs:
           mask: Annotated[Path, ImageSpec()] = Template("{image.stem}_mask.tif")

       def process_row(self, arguments):
           ...

   segment = Segment()
   with Workflow(engine="wetlands") as wf:
       wf.get_environment(segment).max_workers = 4    # 4 worker processes
       images = files(path="/data", pattern="*.tif")
       masks = segment(image=images["path"])
       wf.compute(masks)

Each worker sees ``CUDA_VISIBLE_DEVICES`` set to its zero-based index
(``0``, ``1``, ``2``, ``3``).

Parsl engine
------------

The implemented engines are ``direct`` and ``wetlands``.
``engine="parsl"`` is reserved for a distributed backend that will translate
``ResourceSpec`` into Parsl resource requests when that backend is implemented.
