Tool Packaging and Versioning
=============================

BioImageFlow distributes tools as standard Python packages. The versioned
loading system allows multiple versions of the same package to coexist in a
single process, enabling reproducible workflows.

Package boundary rules
----------------------

Use a package when a tool is reused across projects,
distributed independently, versioned, or carries non-trivial dependencies or
runtime assets. Keep package boundaries explicit:

- A package owns its tools, reusable workflow factories, tests, fixtures, documentation,
  examples, and small runtime assets.
- Package examples must import from that package.
- Each public tool and workflow exported by the package must have tests.
- ``ProcessingTool`` modules may import only the standard library and
  ``bioimageflow-core`` at module import time. Tool-specific dependencies
  are imported inside ``process_row`` or ``process_batch``.
- ``DataFrameTool`` modules belong to the main process and may import
  ``bioimageflow``, pandas, and other main-process dependencies at module
  import time.

Package layout
--------------

A tool package is a standard Python package with a ``pyproject.toml``:

For a practical package scaffold that includes package-owned docs and tests,
see :doc:`custom_tool_package`.

.. code-block:: text

   my_tools/
     pyproject.toml
     my_tools/
       __init__.py
       segmenter.py
       loader.py
       pipeline.py       # optional build_workflow factory
       utils/
         __init__.py
         filters.py

.. code-block:: toml

   # pyproject.toml
   [project]
   name = "my-tools"
   version = "1.0.0"
   requires-python = ">=3.10"
   dependencies = ["bioimageflow-core"]

   [build-system]
   requires = ["hatchling"]
   build-backend = "hatchling.build"

.. _relative-imports:

Relative imports are mandatory
------------------------------

All intra-package imports **must be relative**. This is the most important
rule for tool packages.

.. code-block:: python

   # my_tools/__init__.py

   # Correct -- always use relative imports within a tool package
   from .segmenter import MySegmenter
   from .utils.filters import apply_filter

   # WRONG -- absolute imports break versioned loading
   # from my_tools.segmenter import MySegmenter

.. warning::

   Absolute intra-package imports (e.g., ``from my_tools.segmenter import X``)
   will silently resolve to whichever version of the package was loaded first,
   mixing code from different versions. Always use relative imports.

**Why this matters:** BioImageFlow loads tool packages into isolated
namespaces so that multiple versions can coexist (e.g.,
``my_tools__1_0_0`` and ``my_tools__2_0_0``). Relative imports resolve
within the correct scoped namespace. Absolute imports bypass the scoping
entirely.

This applies everywhere in the package:

- ``__init__.py``
- Tool modules importing from sibling modules
- ``build_workflow`` factories importing tool classes
- Utility modules importing from other utility modules

Example tool package
--------------------

ProcessingTool
^^^^^^^^^^^^^^

.. code-block:: python

   # my_tools/segmenter.py
   from bioimageflow_core.tool import ProcessingTool, IOModel
   from bioimageflow_core.environment import GENERAL_ENV

   class MySegmenter(ProcessingTool):
       display_name = "My Segmenter"
       environment = GENERAL_ENV

       class Inputs(IOModel):
           image: str
           threshold: float = 0.5

       class Outputs(IOModel):
           mask: str

       def process_row(self, arguments):
           # ... processing logic ...
           return self.Outputs(mask=output_path)

Note that ``process_row`` must return a ``self.Outputs(...)`` instance, not a
raw dict. See :doc:`custom_tool` for more details on writing tools.
Tool-specific imports such as ``skimage`` or ``torch`` belong inside
``process_row`` / ``process_batch`` so the package can be imported for schema
inspection without the worker environment installed.

Workflow factory
^^^^^^^^^^^^^^^^

Workflow modules compose tools through an exact ``build_workflow`` factory.
They use relative imports for tools owned by the same package:

.. code-block:: python

   # my_tools/pipeline.py
   from pathlib import Path

   from bioimageflow import Workflow
   from .segmenter import MySegmenter       # relative import

   WORKFLOW_DIRECTORY = Path(__file__).resolve().parent

   def build_workflow(
       *,
       storage_path: str | Path = WORKFLOW_DIRECTORY / "results",
   ) -> Workflow:
       workflow = Workflow(
           name="segment_pipeline",
           display_name="Segment Pipeline",
           storage_path=storage_path,
       )
       with workflow:
           image = workflow.input("image", str, id="input-image")
           threshold = workflow.input("threshold", float, default=0.5, id="input-threshold")
           result = MySegmenter()(image=image, threshold=threshold, name="segment")
           workflow.output("mask", result["mask"], id="output-mask")
       return workflow

__init__.py
^^^^^^^^^^^

Re-export tools for convenient access:

.. code-block:: python

   # my_tools/__init__.py
   from .segmenter import MySegmenter
   from .loader import ImageLoader
   from .pipeline import build_workflow

Testing requirements
--------------------

Every package must ship tests with the package source. For each public tool,
cover:

- schema serialization for ``Inputs`` and ``Outputs``;
- construction-time validation for valid and invalid bindings;
- one successful execution path with tiny synthetic or committed public
  fixtures;
- output template resolution and returned ``Outputs`` instances;
- expected failures for invalid parameters, missing files, or unsupported
  data.

For each public workflow factory, cover graph construction, public interface,
``Workflow.to_dict`` / ``Workflow.from_dict`` round-trip when supported, and
one end-to-end execution on public or synthetic data. Tests should run
without network access unless the package explicitly tests a downloader, in
which case mock the network boundary.

Using versioned packages
------------------------

Workflow-local custom tools do not need to become packages just to make
a workflow shareable: ``Workflow.export(path)`` embeds the workflow-local
``tools/`` package in the workflow JSON. Package a tool when it should be
reused by multiple workflows, distributed independently, versioned, or
installed with non-trivial assets and dependencies.

Loading a single version
^^^^^^^^^^^^^^^^^^^^^^^^

For shareable workflow scripts, declare dependencies using
`PEP 723 <https://peps.python.org/pep-0723/>`_ inline metadata.
``require_tool_packages`` parses the metadata, installs missing packages,
and registers canonical names so standard imports work:

.. code-block:: python

   # /// script
   # dependencies = [
   #   "my-tools==1.0.0",
   # ]
   # ///

   from bioimageflow import Workflow, configure_wetlands, require_tool_packages

   configure_wetlands(wetlands_instance_path="./wetlands")
   require_tool_packages(__file__)

   # Normal imports work after require_tool_packages
   from my_tools import MySegmenter

   with Workflow(storage_path="./results") as wf:
       result = MySegmenter()(image=raw["path"])
       wf.compute(result)

Loading multiple versions side by side
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Use ``load_versioned_package`` and ``resolve_tool_class`` to load two
versions of the same package simultaneously:

.. code-block:: python

   from bioimageflow import Workflow, load_versioned_package
   from bioimageflow.tool_loader import resolve_tool_class
   from bioimageflow_common_tools import Concat

   v1 = load_versioned_package("my_tools", "1.0.0")
   v2 = load_versioned_package("my_tools", "2.0.0")

   # Resolve distinct class objects from each version
   SegV1 = resolve_tool_class("my_tools", "1.0.0", "my_tools.segmenter", "MySegmenter")
   SegV2 = resolve_tool_class("my_tools", "2.0.0", "my_tools.segmenter", "MySegmenter")

   assert SegV1 is not SegV2  # different classes

   with Workflow(storage_path="./results") as wf:
       result_v1 = SegV1()(image=raw["path"])
       result_v2 = SegV2()(image=raw["path"])
       merged = Concat()(result_v1, result_v2)
       wf.compute(merged)

Cleanup:

.. code-block:: python

   from bioimageflow import unload_versioned_package

   unload_versioned_package("my_tools", "1.0.0")
   unload_versioned_package("my_tools", "2.0.0")

Tool store
----------

Versioned packages are installed in the BioImageFlow tool store.
The path is resolved from ``BIOIMAGEFLOW_TOOL_STORE``, then
``BIOIMAGEFLOW_HOME / "tool_packages"``, then
``~/.bioimageflow/tool_packages``:

.. code-block:: text

   ~/.bioimageflow/tool_packages/
     my_tools/
       1.0.0/
         my_tools/
           __init__.py
           segmenter.py
           ...
       2.0.0/
         my_tools/
           __init__.py
           segmenter.py
           ...

Packages are installed automatically by ``require_tool_packages`` or
``Workflow.load()`` when version info is present in a serialized workflow.

``require_tool_packages`` uses Wetlands' Pixi installation to run
``pip install --target`` for missing packages. If a script needs a
project-local Wetlands directory, call ``configure_wetlands()`` before
``require_tool_packages()``; otherwise the default instance path is used.

Programmatic tool enumeration
-----------------------------

For programmatic tool enumeration — populating a GUI tool palette, building a
plugin index, or wiring schema serializers into a host process — prefer
``ToolRegistry`` over the lower-level ``load_versioned_package`` /
``resolve_tool_class`` pair. ``ToolRegistry`` wraps both and exposes
``register_package``, ``get_class``, ``get_metadata``, and ``list_tools``.
See :doc:`/gui/tool_registry` for the full surface.
