ToolRegistry
============

:class:`~bioimageflow.ToolRegistry` is the single object that wraps
:func:`~bioimageflow.load_versioned_package`,
:func:`~bioimageflow.tool_loader.resolve_tool_class`, and the schema
serializers. Hosts use it to populate tool palettes, build plugin
indexes, and serve schemas to inline form widgets.

The registry has two discovery surfaces:

- ``register_package(name, version)`` indexes tools from an installed,
  versioned tool package.
- ``register_workflow(workflow_or_data)`` indexes workflow-local custom
  tools carried by one live workflow or exported workflow dict.

install vs register
-------------------

Two methods drive the registry — and the distinction is critical for
performance:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Method
     - Behaviour
   * - ``install_package(name, version, *, install_dependencies=True)``
     - **Slow, network-bound.** Downloads, installs, and unpacks a
       versioned package into the tool store. Calls do not load or
       index anything.
   * - ``register_package(name, version)``
     - **Fast, in-process.** Loads an already-installed package via
       :func:`load_versioned_package` and indexes every BaseTool /
       executable concrete tool subclass it discovers; incomplete reusable families are not runnable palette entries. Raises
       :class:`FileNotFoundError` if the package is not present in the
       store.

Register packages before serving hot validation paths.
Sessions, validators and keystroke-rate previews should use captured registry/schema facts rather than repeatedly importing packages or installing dependencies.
Trigger ``install_package`` from an explicit user-authorized install action.
Hosts with compatible main-process dependencies may pass
``install_dependencies=False`` to install only the tool distribution.
The default installs its declared dependencies.

Workflow custom tools
---------------------

Custom tools that live with a workflow are not promoted to packages just
to make the workflow portable. ``Workflow.export(path)`` embeds their
workflow-local ``tools/`` package in the exported JSON, and the registry
can discover those tools for that specific workflow:

.. code-block:: python

   from bioimageflow import ToolRegistry, Workflow
   from pathlib import Path

   reg = ToolRegistry()

   wf = Workflow.load("workflow.json", storage_path="./results")
   metas = reg.register_workflow(wf)

   # Or register directly from the exported dict before materializing it.
   import json
   workflow_data = json.loads(Path("workflow.json").read_text())
   metas = reg.register_workflow(workflow_data)

``register_workflow`` only indexes custom tools carried by the workflow.
It does not install or register package references; keep using
``register_package`` for package-backed tools.

ToolMetadata
------------

``register_package`` returns ``list[ToolMetadata]`` — one entry per
discovered tool class:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Field
     - Description
   * - ``package``
     - Package import name (e.g., ``"my_tools"``).
   * - ``version``
     - Pinned version string (e.g., ``"1.2.3"``).
   * - ``module``
     - Canonical module path (e.g., ``"my_tools.alpha"``).
   * - ``class_name``
     - Class name as written in the source.
   * - ``inputs_schema``
     - Output of
       :func:`~bioimageflow.validation.serialize_input_schema` —
       JSON-safe input field descriptions.
   * - ``outputs_schema``
     - Output of
       :func:`~bioimageflow.validation.serialize_output_schema`.
   * - ``display_name``
     - Human-readable label declared on the class (or class name
       fallback).
   * - ``tags``
     - Free-form tag tuple from the class (empty if not declared).

The schemas are captured during registration; later metadata lookup does not rerun scientific tool work.
The accepted contract keeps caller-facing schema/default snapshots detached from registry authority and preserves schema failures as diagnostics rather than empty success.
Concrete ProcessingTool admission requires an environment, IOModel outputs and explicit mapped/collective row semantics.
DataFrameTools have no worker environment or RowConsumption requirement and may declare static, Passthrough or dynamic output schemas without an Outputs class.
Per-tool facts such as row_consumption, accepts_upstream and dynamic_outputs are described by ``serialize_tool_metadata`` in :doc:`/specs` §2.4.
Exact current snapshot/error API conformance remains a test/code-review obligation.

Lookups
-------

After registration, four methods drive lookups:

- ``get_class(class_name)`` — returns the class object, or ``None``.
- ``get_metadata(class_name)`` — returns the cached
  :class:`~bioimageflow.ToolMetadata`, or ``None``.
- ``list_tools()`` — returns every registered metadata in insertion
  order (suitable for tool palettes).
- ``forget(class_name)`` — drops a class from the registry. No-op when
  unknown.

Multiple versions
-----------------

An unqualified class-name lookup is a convenience for the current palette; it is not executable identity.
Package/version/module/class or captured custom-source identity addresses the exact executable declaration.
Same-current-process versions remain distinct classes even when a palette convenience lookup selects a recent registration:

.. code-block:: python

   reg = ToolRegistry()
   reg.register_package("my_tools", "1.0.0")
   reg.register_package("my_tools", "2.0.0")
   reg.get_class("Segmenter")          # the v2.0.0 class

Use exact package/version/module/class resolution when a workflow addresses a specific executable, rather than creating a second independent registry authority.
Scoped package names alone do not prove transitive dependency isolation; incompatible process-global dependency authority needs explicit refusal under :doc:`/specs` §3.10.
Installed Python is trusted code: registration imports it, while cached palette lookups do not.

Worked example: GUI startup populates a tool palette
----------------------------------------------------

A host explicitly installs a curated set of packages, then registers each before serving the palette:

.. code-block:: python

   from bioimageflow import ToolRegistry

   reg = ToolRegistry()

   # First-run only — slow, runs once.
   for pkg, ver in REQUIRED_TOOL_PACKAGES:
       reg.install_package(pkg, ver)

   # Explicit registration — imports trusted declarations once before palette lookup.
   for pkg, ver in REQUIRED_TOOL_PACKAGES:
       reg.register_package(pkg, ver)

   # Build the tool palette.
   palette = [
       {
           "label": meta.display_name,
           "tags":  meta.tags,
           "schema": meta.inputs_schema,
       }
       for meta in reg.list_tools()
   ]

The palette is populated entirely from cached metadata — no Python
imports, no schema introspection on the hot path.
