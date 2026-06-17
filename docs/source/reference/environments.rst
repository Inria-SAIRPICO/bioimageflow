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

Use it for tools that only need standard scientific packages. Tools
with specialized dependencies declare their own
``EnvironmentSpec``.

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
     - Reserved scheduling hint. Direct and Wetlands v1 do not enforce
       it; use ``Workflow(max_workers=...)`` or
       ``Workflow.get_environment(...).max_workers`` for local worker
       pool sizing.
   * - ``memory``
     - ``None``
     - Optional string hint for system memory.

Current local engines read the spec for one effect:

- When ``gpu >= 1`` and the environment has no explicit ``worker_env``,
  the engine auto-installs
  ``worker_env = lambda i: {"CUDA_VISIBLE_DEVICES": str(i)}`` so each
  Wetlands worker is pinned to a distinct GPU.

The Parsl engine is reserved for richer scheduling — full ``cpu`` /
``memory`` / ``max_concurrent`` semantics ship with that
implementation.

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

Wetlands
--------

The actual environment provisioning is handled by `Wetlands
<https://github.com/wetlands-team/wetlands>`_. BioImageFlow ships an
adapter that converts ``EnvironmentSpec`` into Wetlands environment
declarations and dispatches ``ProcessingTool.process_row`` calls into
worker processes. Hosts that need to inspect environment state at
runtime should use Wetlands' own APIs; the BioImageFlow adapter is an
implementation detail.

Specs.md Appendix A is the dedicated Wetlands page; the parallelism
tutorial (:doc:`/tutorials/parallelism`) covers how the proxy fields
interact with row-level parallelism.
