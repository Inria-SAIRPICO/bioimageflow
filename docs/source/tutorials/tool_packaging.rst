Tool Packaging and Versioning
=============================

BioImageFlow distributes tools as standard Python packages. The versioned
loading system allows multiple versions of the same package to coexist in a
single process, enabling reproducible workflows and gradual migrations.

Package layout
--------------

A tool package is a standard Python package with a ``pyproject.toml``:

.. code-block:: text

   my_tools/
     pyproject.toml
     my_tools/
       __init__.py
       segmenter.py
       loader.py
       pipeline.py       # optional SubWorkflow
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
- SubWorkflow ``build()`` methods importing tool classes
- Utility modules importing from other utility modules

Example tool package
--------------------

ProcessingTool
^^^^^^^^^^^^^^

.. code-block:: python

   # my_tools/segmenter.py
   from bioimageflow_core.tool import ProcessingTool, IOModel
   from bioimageflow_core.environment import EnvironmentSpec

   class MySegmenter(ProcessingTool):
       display_name = "My Segmenter"

       environment = EnvironmentSpec(
           name="my_tools",
           dependencies={"pip": ["numpy", "scikit-image"], "python": "3.12"},
       )

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

SubWorkflow
^^^^^^^^^^^

SubWorkflows compose tools into reusable sub-pipelines. They must also use
relative imports to reference tools from the same package:

.. code-block:: python

   # my_tools/pipeline.py
   from bioimageflow.sub_workflow import SubWorkflow
   from bioimageflow_core.tool import IOModel
   from .segmenter import MySegmenter       # relative import
   from .loader import ImageLoader          # relative import

   class SegmentPipeline(SubWorkflow):
       display_name = "Segment Pipeline"

       class Inputs(IOModel):
           image: str
           threshold: float = 0.5

       class Outputs(IOModel):
           mask: str

       def build(self, inputs):
           seg = MySegmenter()
           result = seg(image=inputs.image, threshold=inputs.threshold)
           return {"mask": result["mask"]}

__init__.py
^^^^^^^^^^^

Re-export tools for convenient access:

.. code-block:: python

   # my_tools/__init__.py
   from .segmenter import MySegmenter
   from .loader import ImageLoader
   from .pipeline import SegmentPipeline

Using versioned packages
------------------------

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

   from bioimageflow import Workflow, require_tool_packages

   require_tool_packages(__file__)

   # Normal imports work after require_tool_packages
   from my_tools import MySegmenter

   with Workflow() as wf:
       result = MySegmenter()(image=raw["path"])
       wf.compute(result)

Loading multiple versions side by side
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Use ``load_versioned_package`` and ``resolve_tool_class`` to load two
versions of the same package simultaneously:

.. code-block:: python

   from bioimageflow import Workflow, Concat, load_versioned_package
   from bioimageflow.tool_loader import resolve_tool_class

   v1 = load_versioned_package("my_tools", "1.0.0")
   v2 = load_versioned_package("my_tools", "2.0.0")

   # Resolve distinct class objects from each version
   SegV1 = resolve_tool_class("my_tools", "1.0.0", "my_tools.segmenter", "MySegmenter")
   SegV2 = resolve_tool_class("my_tools", "2.0.0", "my_tools.segmenter", "MySegmenter")

   assert SegV1 is not SegV2  # different classes

   with Workflow() as wf:
       old = SegV1()(image=raw["path"])
       new = SegV2()(image=raw["path"])
       merged = Concat()(old, new)
       wf.compute(merged)

Cleanup:

.. code-block:: python

   from bioimageflow import unload_versioned_package

   unload_versioned_package("my_tools", "1.0.0")
   unload_versioned_package("my_tools", "2.0.0")

Tool store
----------

Versioned packages are installed in ``~/.bioimageflow/tool_packages/``:

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
The store path can be overridden via the ``BIOIMAGEFLOW_TOOL_STORE``
environment variable.
