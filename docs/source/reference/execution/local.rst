Local Execution
===============

The default local engine runs each :class:`~bioimageflow_core.ProcessingTool` in the isolated environment declared by its tool class.
BioImageFlow provisions those environments and uses a pool of worker processes for row tasks.

Basic configuration
-------------------

.. code-block:: python

   from bioimageflow import Workflow, configure_wetlands

   configure_wetlands(root="./wetlands")

   workflow = Workflow(
       storage_path="./results",
       engine="wetlands",
       execution="parallel",
       max_workers=4,
   )

``engine="wetlands"`` and ``execution="parallel"`` are the defaults.
``max_workers`` is the default pool size for each environment and defaults to one.

Per-environment settings
------------------------

:meth:`~bioimageflow.Workflow.get_environment` returns the runtime configuration shared by tools with the same environment name:

.. code-block:: python

   workflow.get_environment(cpu_tool).max_workers = 8
   workflow.get_environment(gpu_tool).max_workers = 1
   workflow.get_environment(gpu_tool).worker_timeout = 900.0

The lookup accepts a ProcessingTool instance, an :class:`~bioimageflow_core.EnvironmentSpec`, or a name string.
Repeated lookups for the same name return the same mutable proxy.

``worker_timeout`` is measured in seconds and defaults to ``None``.
It is intended for native code that can deadlock, not as an ordinary task-duration policy.

Scheduling behavior
-------------------

In parallel mode, independent reachable ProcessingTool nodes may overlap and rows of one node may occupy multiple workers.
Tools sharing one environment also share its worker pool.
The pool size is not multiplied by the number of nodes.

``DataFrameTool.transform()`` and ``merge_dataframes()`` run in the orchestrator and are not submitted to worker processes.
A ProcessingTool that implements whole-node ``process_batch()`` occupies one worker task for the batch.

The local engine enforces the effective ``max_concurrent`` limit for each node.
CPU, GPU, memory, and GPU-memory values remain portable planning requirements; the local engine does not perform resource placement or assign per-worker GPU visibility.
See :doc:`resources` for the complete resource contract.

Environment storage
-------------------

Call :func:`~bioimageflow.configure_wetlands` before workflow loading, tool-package installation, or execution when the defaults are unsuitable.
The environment root is resolved in this order when it is not passed explicitly:

1. ``BIOIMAGEFLOW_WETLANDS``
2. ``BIOIMAGEFLOW_HOME / "wetlands"``
3. ``~/.bioimageflow/wetlands``

A workflow may also carry per-run settings:

.. code-block:: python

   workflow = Workflow(
       storage_path="./results",
       wetlands_config={
           "root": "./wetlands",
           "termination_grace": 7.0,
       },
   )

Process-wide and workflow values are merged when the shared environment manager is first created, with workflow values taking precedence.
Later process-wide configuration is ignored with a warning after initialization.

Resource ownership
------------------

Workflow-created engines use execution-scoped ownership by default, so worker environments stop after ``compute()`` or after a stepped iterator finishes or closes.
Keep workers warm across calls by retaining an engine:

.. code-block:: python

   with workflow.create_engine(resource_lifetime="engine") as engine:
       first = workflow.compute(engine=engine)
       second = workflow.compute(engine=engine)

The lifetime values are:

- ``"execution"``: stop execution-owned workers after each execution;
- ``"engine"``: retain workers until the engine closes;
- ``"external"``: use an injected :class:`~bioimageflow.env_manager.WetlandsEnvManager`, whose owner performs shutdown.

An externally owned manager can serve several engines:

.. code-block:: python

   from bioimageflow import ResourceLifetime, WetlandsEnvManager

   manager = WetlandsEnvManager()
   engine = workflow.create_engine(
       env_manager=manager,
       resource_lifetime=ResourceLifetime.EXTERNAL,
   )
   try:
       workflow.compute(engine=engine)
   finally:
       engine.close()
       manager.shutdown_all()

The public manager supports ``stop(name)``, ``is_running(name)``, ``running_environments()``, and idempotent ``shutdown_all()``.

Direct execution
----------------

``engine="direct"`` invokes processing methods in the orchestrator process without environment isolation or worker dispatch.
It is useful for focused tests and very small debugging examples.
It is not the normal way to execute workflows whose tools declare isolated dependencies.

.. code-block:: python

   with Workflow(
       storage_path="./debug-results",
       engine="direct",
       execution="sequential",
   ) as workflow:
       source = files(path="./images", pattern="*.tif")
       target = tool(image=source["path"])
       result = workflow.compute(target)

Direct execution is sequential within a node.
Portable resource declarations are exposed to planning but do not allocate or constrain orchestrator resources.
