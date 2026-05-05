Parallelism
===========

BioImageFlow's default engine runs work in parallel out of the box —
independent DAG branches concurrently, and rows of a
:class:`~bioimageflow_core.ProcessingTool` in parallel across worker
processes. This page covers the knobs that size that parallelism.

What's parallel by default
--------------------------

Two sources of concurrency:

- **Independent DAG branches** run **concurrently**. Two ``ProcessingTool``
  nodes whose declared environments differ can launch their Wetlands
  process pools side-by-side.
- **Rows of a single ProcessingTool** run **in parallel** across the
  worker processes of that tool's environment pool. Pool sizing is
  controlled by ``max_workers`` (see below).

DataFrameTools always run in the main process; their
``transform`` / ``merge_dataframes`` calls are not parallelised.

Workflow-level baseline
-----------------------

``Workflow(max_workers=N)`` sets the default per-environment worker count
for tools that don't override it:

.. code-block:: python

   from bioimageflow import Workflow

   with Workflow(max_workers=4) as wf:
       ...

The default is ``max_workers=1`` — a single worker per environment.

Per-environment overrides
-------------------------

:meth:`~bioimageflow.Workflow.get_environment` returns a mutable
:class:`~bioimageflow.WorkflowEnvironment` proxy keyed by
:class:`~bioimageflow_core.EnvironmentSpec` name. Setting
``max_workers`` on the proxy overrides the workflow default for tools in
that environment:

.. code-block:: python

   from bioimageflow_core import GENERAL_ENV

   cellpose = CellposeSAM()                  # has its own EnvironmentSpec
   filter_tool = FilterByArea()              # uses GENERAL_ENV

   with Workflow(max_workers=4) as wf:
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
   with Workflow() as wf:
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

The default engine honours ``gpu``: when a tool's ``ResourceSpec`` has
``gpu >= 1`` and no explicit ``worker_env`` is set on its environment,
the engine auto-installs
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

engine="sequential" for debugging
---------------------------------

Constructing a workflow with ``engine="sequential"`` and ``max_workers=1``
gives deterministic, single-threaded execution that is easier to debug:

.. code-block:: python

   with Workflow(engine="sequential", max_workers=1) as wf:
       ...

Use it when chasing a non-deterministic bug; switch back to the default
once the bug is reproduced.

Worked example: a GPU tool with row-level parallelism
-----------------------------------------------------

A tool declares ``ResourceSpec(gpu=1)`` and lives in its own environment.
The workflow runs four rows of it in parallel, each pinned to a distinct
GPU:

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
   with Workflow() as wf:
       wf.get_environment(segment).max_workers = 4    # 4 worker processes
       images = files(path="/data", pattern="*.tif")
       masks = segment(image=images["path"])
       wf.compute(masks)

Each worker sees ``CUDA_VISIBLE_DEVICES`` set to its zero-based index
(``0``, ``1``, ``2``, ``3``).

Parsl engine
------------

``engine="parsl"`` is reserved for a future Parsl-backed engine that
will translate ``ResourceSpec`` into Parsl resource requests. The
default engine is the only implemented engine today; the wire-format
``engine`` field is preserved through ``to_dict`` / ``from_dict`` so
graphs authored against future engines round-trip.
