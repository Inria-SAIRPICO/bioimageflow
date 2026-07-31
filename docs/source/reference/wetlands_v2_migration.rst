Wetlands 2 migration
====================

BioImageFlow now requires ``wetlands>=2.0.0,<3``.
Direct execution is unaffected, while the Wetlands backend uses the new public construction, provisioning, pool, task, event, failure, and cleanup contracts.

Configuration
-------------

Configure the manager root and optional Pixi executable with:

.. code-block:: python

   from bioimageflow import configure_wetlands

   configure_wetlands(
       root="./wetlands",
       pixi_executable="/opt/bin/pixi",
       network="online",
       termination_grace=10,
   )

``wetlands_instance_path`` remains a migration alias for ``root`` and ``conda_path`` remains an alias for ``pixi_executable``.
New code should use the canonical names.
Wetlands 1 ``main_conda_environment_path``, manager selection, constructor debug mode, and injected managers are rejected.

Environment recipes
-------------------

Tool authors continue to declare :class:`bioimageflow_core.EnvironmentSpec`.
BioImageFlow translates it to Wetlands 2's immutable ``EnvironmentSpec`` and typed local-package entries, normalizes channel-qualified Conda dependencies, and adds the compatible ``bioimageflow-core`` runtime requirement.
No Wetlands value leaks into graph serialization or tool APIs.

Execution lifecycle
-------------------

The migration replaces the Wetlands 1 lifecycle:

.. list-table::
   :header-rows: 1

   * - Wetlands 1
     - Wetlands 2 integration
   * - ``EnvironmentManager.create()``
     - ``EnvironmentManager.provision(...).wait_for()``
   * - ``environment.launch(max_workers=...)``
     - ``environment.start(workers=..., worker_timeout=...)``
   * - filename/module proxy calls
     - ``pool.submit_import("bioimageflow_core.worker:execute_processing_task", ...)``
   * - ``TaskStatus`` and string errors
     - ``ExecutionState`` and structured ``ExecutionFailure``
   * - ``environment.exit()``
     - ``pool.close()`` and ``manager.close()``

Provisioning remains lazy.
Cached workflows do not create an environment or pool.
The selected :class:`bioimageflow.ResourceLifetime` continues to decide whether BioImageFlow closes resources after one execution or retains them for the engine session.

Workers and resources
---------------------

``Workflow.max_workers`` remains the default Wetlands 2 pool size, and ``workflow.get_environment(tool).max_workers`` may override it for one environment.
``worker_timeout`` is forwarded when the pool starts.

Wetlands 2 does not expose the former application-facing per-worker environment callback.
Setting ``WorkflowEnvironment.worker_env`` therefore raises an actionable migration error before provisioning.
BioImageFlow no longer infers or assigns ``CUDA_VISIBLE_DEVICES``.
Use the environment or external process launcher to configure GPU visibility.

Portable :class:`bioimageflow.NodeResourceOverrides` are independent from pool configuration.
Their effective ``max_concurrent`` value bounds row submission to the Wetlands pool, while CPU, GPU, memory, and GPU-memory values remain available to public planning and Parsl placement.

Failure and progress behavior
-----------------------------

BioImageFlow adapts Wetlands 2 public task events to its existing :class:`bioimageflow.ProgressEvent` stream.
Structured Wetlands failures become :class:`bioimageflow.WorkerTaskError` and the engine-neutral :class:`bioimageflow.NodeFailureDiagnostic`.
Tool-side progress through the BioImageFlow worker protocol remains unchanged.

Migration checklist
-------------------

- Raise the dependency to ``wetlands>=2.0.0,<3``.
- Rename ``configure_wetlands(wetlands_instance_path=...)`` to ``configure_wetlands(root=...)``.
- Rename ``conda_path`` to ``pixi_executable`` when an explicit Pixi binary is required.
- Remove ``worker_env`` callbacks and configure device visibility outside BioImageFlow.
- Regenerate any Wetlands-supplied lockfiles according to the Wetlands 2 manifest rules.
- Run one uncached Wetlands workflow to verify provisioning and worker-package availability.
