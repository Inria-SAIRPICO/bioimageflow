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
   * - ``install_package(name, version)``
     - **Slow, network-bound.** Downloads, installs, and unpacks a
       versioned package into the tool store. Calls do not load or
       index anything.
   * - ``register_package(name, version)``
     - **Fast, in-process.** Loads an already-installed package via
       :func:`load_versioned_package` and indexes every BaseTool /
       executable tool subclass it discovers. Raises
       :class:`FileNotFoundError` if the package is not present in the
       store.

Hot validation paths (sessions, validators, keystroke-rate previews)
must call ``register_package`` only. Trigger ``install_package`` from
a user-initiated action (a "Install plugin" button, an explicit dialog).

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

The schemas are computed once per ``register_package`` call; subsequent
``get_metadata`` lookups are dict reads.

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

The registry is a flat ``class_name → metadata`` index, but
``load_versioned_package`` keeps versions isolated under scoped
namespaces. When two versions of the same package register classes with
the same name, the **last registered wins** in the registry's lookup
table, but the underlying classes remain distinct objects:

.. code-block:: python

   reg = ToolRegistry()
   reg.register_package("my_tools", "1.0.0")
   reg.register_package("my_tools", "2.0.0")
   reg.get_class("Segmenter")          # the v2.0.0 class

For an editor that needs to address both versions simultaneously, build
two registries (one per version) or fall back to
``resolve_tool_class("my_tools", "1.0.0", ...)`` directly.

Worked example: GUI startup populates a tool palette
----------------------------------------------------

A host installs a curated set of packages once, then registers each on
every startup:

.. code-block:: python

   from bioimageflow import ToolRegistry

   reg = ToolRegistry()

   # First-run only — slow, runs once.
   for pkg, ver in REQUIRED_TOOL_PACKAGES:
       reg.install_package(pkg, ver)

   # Every startup — fast, in-process.
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
