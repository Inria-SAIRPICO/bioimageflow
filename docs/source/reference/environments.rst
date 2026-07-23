Environments
============

This page collects the surface around tool environments — declaration,
resource requirements, and per-workflow runtime configuration. The
parallelism tutorial (:doc:`/tutorials/parallelism`) walks through the
common usage patterns; this reference is the load-bearing one for
``EnvironmentSpec``, ``GENERAL_ENV``, ``ResourceSpec``, and
``WorkflowEnvironment``.

EnvironmentSpec
---------------

:class:`~bioimageflow_core.EnvironmentSpec` is a frozen dataclass
declaring the dependencies a tool needs:

.. code-block:: python

   from bioimageflow_core import EnvironmentSpec

   torch_env = EnvironmentSpec(
       name="torch",
       dependencies={
           "python": "3.12",
           "pip": ["torch>=2.0", "torchvision"],
       },
   )

The ``name`` is a stable identifier — multiple tools sharing the same
``name`` must declare **identical** dependencies, or
:class:`~bioimageflow_core.EnvironmentMismatchError` is raised when a
workflow containing those reachable tools is planned or computed.
The ``dependencies`` dict mirrors the Wetlands
schema; the most common keys are ``python`` and ``pip``.

GENERAL_ENV
-----------

:data:`~bioimageflow_core.GENERAL_ENV` is the canonical "scientific
Python" environment — Python 3.12 with numpy, scipy, scikit-image,
imageio, tifffile, and Pillow:

.. code-block:: python

   from bioimageflow_core import GENERAL_ENV

   class MyTool(ProcessingTool):
       environment = GENERAL_ENV
       ...

Use it for tools that only need standard scientific packages or the Python
standard library.
This includes simple source and utility tools such as HTTP downloads implemented with ``urllib``, path normalization, CSV/table glue, and small file operations.
Do not create one-off environments for these tasks.
Tools with specialized dependencies such as Cellpose, StarDist, SimpleITK, BioIO, external command-line tools, model runtimes, or packages not included in ``GENERAL_ENV`` declare their own ``EnvironmentSpec``.

ResourceSpec
------------

:class:`~bioimageflow_core.ResourceSpec` declares the resources a
tool needs per row:

.. list-table::
   :header-rows: 1
   :widths: 22 12 66

   * - Field
     - Default
     - Meaning
   * - ``cpu``
     - ``1``
     - CPU cores per row.
   * - ``gpu``
     - ``0``
     - GPUs per row.
   * - ``gpu_memory``
     - ``None``
     - Optional string hint, e.g. ``"8GB"``.
   * - ``max_concurrent``
     - ``0``
     - Parsl bound on unfinished row or chunk tasks for one processing node.
       Direct and Wetlands do not enforce it; use ``Workflow(max_workers=...)`` or ``Workflow.get_environment(...).max_workers`` for Wetlands worker pool sizing.
   * - ``memory``
     - ``None``
     - Optional string hint for system memory.

Wetlands reads the spec for one effect:

- When ``gpu >= 1`` and the environment has no explicit ``worker_env``, the engine auto-installs ``worker_env = lambda i: {"CUDA_VISIBLE_DEVICES": str(i)}`` so each Wetlands worker is pinned to a distinct GPU.

Parsl converts the resource declaration into a canonical requirement and validates it against the selected :class:`~bioimageflow.ExecutorBinding`.
The binding's :class:`~bioimageflow.WorkerSlotCapacity` must satisfy the requested ``cpu``, ``gpu``, ``memory``, and ``gpu_memory``.
``max_concurrent`` combines with :class:`~bioimageflow.ParslTaskPolicy.max_in_flight` to bound unfinished tasks; it is not part of slot capacity.
See :doc:`parsl` for executor attestations and deterministic routing.

WorkflowEnvironment and get_environment
---------------------------------------

:meth:`Workflow.get_environment <bioimageflow.Workflow.get_environment>`
returns a mutable :class:`~bioimageflow.WorkflowEnvironment` proxy
keyed by ``EnvironmentSpec.name``:

.. code-block:: python

   wenv = wf.get_environment(my_tool)        # by ProcessingTool
   wenv = wf.get_environment(GENERAL_ENV)    # by EnvironmentSpec
   wenv = wf.get_environment("torch")        # by name string

The proxy carries three runtime fields:

- ``max_workers`` (int, default ``0`` meaning "use workflow default")
  — pool size for this environment.
- ``worker_env`` (callable, default ``None``) —
  ``(worker_index: int) -> dict[str, str]`` injecting per-worker env
  vars at spawn time.
- ``worker_timeout`` (float, default ``None``) — last-resort safety
  timeout per row dispatch.

Multiple ``get_environment`` calls with the same target name return
the **same** proxy — edits propagate.

Wetlands configuration
----------------------

BioImageFlow uses one shared Wetlands
``EnvironmentManager`` per Python process. Configure it once near the
top of a script, before calling ``require_tool_packages()``,
``Workflow.load()``, or ``Workflow.compute()``:

.. code-block:: python

   from bioimageflow import Workflow, configure_wetlands

   configure_wetlands(wetlands_instance_path="./wetlands")

``wetlands_instance_path`` is where Wetlands stores its process state,
logs, debug port registry, and bundled Pixi or Micromamba installation.
When ``conda_path`` is omitted, Wetlands installs Pixi under
``<wetlands_instance_path>/pixi``.

If no explicit path is configured, BioImageFlow resolves the default
Wetlands instance path in this order:

1. ``BIOIMAGEFLOW_WETLANDS``
2. ``BIOIMAGEFLOW_HOME / "wetlands"``
3. ``~/.bioimageflow/wetlands``

You can also pass Wetlands settings to a single workflow:

.. code-block:: python

   wf = Workflow(
       storage_path="./results",
       wetlands_config={
           "wetlands_instance_path": "./wetlands",
           "debug": True,
       },
   )

Process-wide configuration and per-workflow configuration are merged
when the shared manager is first created, with per-workflow values
taking precedence. After the manager exists, later calls to
``configure_wetlands()`` are ignored with a warning. This matters for
shareable scripts because ``require_tool_packages()`` may initialize
Wetlands while installing missing tool packages.

Environment lifetime and ownership
----------------------------------

Workflow-created engines use execution-scoped ownership by default: Wetlands workers stop after every ``compute()`` or completed or closed ``compute_steps()`` call.
Applications that execute repeatedly can create and retain an engine explicitly:

.. code-block:: python

   from bioimageflow import Workflow

   wf = Workflow.load("workflow.json")
   with wf.create_engine(resource_lifetime="engine") as engine:
       first = wf.compute(engine=engine)
       second = wf.compute(engine=engine)
       print(engine.environment_manager.running_environments())

The public :class:`~bioimageflow.engine.ResourceLifetime` values define ownership:

- ``"execution"`` stops all environments after every ``execute()`` or ``execute_steps()`` call, including failed and cancelled calls.
- ``"engine"`` retains environments across calls and stops them when :meth:`~bioimageflow.engine.DefaultEngine.close` is called or the engine context exits.
- ``"external"`` requires an injected :class:`~bioimageflow.env_manager.WetlandsEnvManager`; neither execution nor engine closure stops that manager.

``DefaultEngine.close()`` and ``SequentialEngine.close()`` are idempotent.
A closed engine cannot be executed again.
``execute_steps()`` follows the same ownership policy as ``execute()``: execution-owned workers stop when its generator finishes or is closed, while engine-owned workers remain warm.

An application can share a manager across workflow engines:

.. code-block:: python

   from bioimageflow import ResourceLifetime, WetlandsEnvManager

   manager = WetlandsEnvManager()
   first_engine = first_workflow.create_engine(
       env_manager=manager,
       resource_lifetime=ResourceLifetime.EXTERNAL,
   )
   second_engine = second_workflow.create_engine(
       env_manager=manager,
       resource_lifetime=ResourceLifetime.EXTERNAL,
   )

   try:
       first_workflow.compute(engine=first_engine)
       second_workflow.compute(engine=second_engine)
       manager.stop("cellpose")
   finally:
       first_engine.close()
       second_engine.close()
       manager.shutdown_all()

The manager exposes ``stop(env_name)``, ``is_running(env_name)``, ``running_environments()``, and idempotent ``shutdown_all()`` so hosts do not need to access ``_envs`` or ``_launch_configs``.
Status means that the adapter tracks an environment as launched; it is not a process-health probe.

Long-running applications should keep one ``WetlandsEnvManager`` for the desired sharing scope and build workflow engines with ``resource_lifetime="external"`` as shown above.
Use ``manager.stop(name)`` for a per-environment stop action and ``manager.shutdown_all()`` during application shutdown.
Alternatively, use one ``resource_lifetime="engine"`` engine per session and close it when that session ends.

Retaining an environment preserves imported modules, CUDA process state, and BioImageFlow's cached tool instances.
``Cellpose3``, ``CellposeSAM``, and ``StarDistSegmenter`` lazily retain one model on each worker-side tool instance, so repeated rows and retained-engine executions with the same model selection reuse its weights.
The cache key is ``model_type`` for Cellpose and ``model_name`` for StarDist; inference settings such as diameter, thresholds, channels, and normalization do not reload weights.
Selecting a different model replaces the cached reference instead of accumulating GPU models, and ``clear_model_cache()`` explicitly invalidates it.
``clear_model_cache()`` operates on the tool instance in the current process; applications invalidate worker-side caches through ``manager.stop(environment_name)`` or engine shutdown.
Other tools that construct heavy objects inside ``process_row`` or ``process_batch`` must implement their own worker-instance cache to gain the same benefit.

Wetlands
--------

The actual environment provisioning is handled by `Wetlands
<https://github.com/wetlands-team/wetlands>`_. BioImageFlow ships an
adapter that converts ``EnvironmentSpec`` into Wetlands environment
declarations and dispatches ``ProcessingTool.process_row`` calls into
worker processes.
Hosts should use the public :class:`~bioimageflow.env_manager.WetlandsEnvManager` lifecycle surface described above for worker ownership and status.
Deeper Wetlands provisioning and process-health details remain implementation-level and should use Wetlands' own APIs when necessary.

Specs.md Appendix A is the dedicated Wetlands page; the parallelism
tutorial (:doc:`/tutorials/parallelism`) covers how the proxy fields
interact with row-level parallelism.
