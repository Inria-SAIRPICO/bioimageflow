Quick Start
===========

This guide walks you through building and running your first BioImageFlow
pipeline in under 5 minutes.

Files created by BioImageFlow
-----------------------------

Before running a workflow, choose where BioImageFlow should write its
state. There are three separate locations:

- ``Workflow(storage_path=...)`` stores workflow outputs, cache
  metadata, and generated assets. If omitted, it defaults to
  ``./bif_data``.
- ``configure_wetlands(wetlands_instance_path=...)`` stores Wetlands'
  environment state: logs, debug port metadata, the bundled Pixi or
  Micromamba installation, and the isolated tool environments. Call it
  before ``Workflow.compute()``, ``Workflow.load()``, or
  ``require_tool_packages()``.
- The tool store holds versioned tool packages installed by
  ``require_tool_packages()`` or ``Workflow.load()``. It defaults to
  ``~/.bioimageflow/tool_packages`` unless ``BIOIMAGEFLOW_HOME`` is set.

For a project-local run, start scripts like this:

.. code-block:: python

   from bioimageflow import Workflow, configure_wetlands

   configure_wetlands(wetlands_instance_path="./wetlands")

   with Workflow(storage_path="./bif_data") as wf:
       ...

If ``configure_wetlands()`` is not called, the Wetlands instance path
is resolved from ``BIOIMAGEFLOW_WETLANDS``, then
``BIOIMAGEFLOW_HOME / "wetlands"``, then ``~/.bioimageflow/wetlands``.
The tool store is resolved from ``BIOIMAGEFLOW_TOOL_STORE``, then
``BIOIMAGEFLOW_HOME / "tool_packages"``, then
``~/.bioimageflow/tool_packages``.

Pick a source
-------------

A workflow starts at a *source node* — a tool that produces the initial
DataFrame without consuming any upstream data. The companion
``bioimageflow_common_tools`` package ships ``Files``, which lists the files in
a directory:

.. code-block:: python

   from bioimageflow_common_tools import Files

   files = Files()

``Files`` is a :class:`~bioimageflow.DataFrameTool` with
``accepts_upstream = False``. Its output is a ``path`` column containing
absolute paths. Source-tool patterns (and how to write your own) are covered
in :doc:`concepts/graph`.

Define a processing tool
------------------------

A :class:`~bioimageflow_core.ProcessingTool` runs on each row independently.
Declare its inputs, outputs, and the environment it needs:

.. code-block:: python

   from pathlib import Path
   from typing import Annotated

   from bioimageflow_core import (
       ProcessingTool, EnvironmentSpec, ImageSpec, Arguments, Template,
   )

   class InvertImage(ProcessingTool):
       display_name = "Invert"
       environment = EnvironmentSpec(
           name="imageio",
           dependencies={"python": "3.10", "pip": ["imageio", "numpy"]},
       )

       class Inputs:
           image: Annotated[Path, ImageSpec()]

       class Outputs:
           inverted: Annotated[Path, ImageSpec()] = Template("{image.stem}_inv.tif")

       def process_row(self, arguments: Arguments) -> "InvertImage.Outputs":
           import imageio.v3 as iio
           img = iio.imread(arguments.image)
           out = 255 - img
           iio.imwrite(arguments.inverted, out)
           return self.Outputs(inverted=arguments.inverted)

Key points:

- ``Inputs`` fields that receive image data use
  ``Annotated[Path, ImageSpec(...)]``.
- ``Outputs`` path fields use ``Template(...)`` defaults for **output path templates**
  (see :doc:`tutorials/output_templating`).
- ``process_row`` receives an :class:`~bioimageflow_core.Arguments` object with
  resolved values for every input and output field.

Build the workflow
------------------

Wire tools together inside a :class:`~bioimageflow.Workflow` context manager:

.. code-block:: python

   from bioimageflow import Workflow, configure_wetlands

   invert = InvertImage()

   configure_wetlands(wetlands_instance_path="./wetlands")

   with Workflow(storage_path="./bif_data", engine="wetlands") as wf:
       images = files(path="/data/raw", pattern="*.tif")
       inverted = invert(image=images["path"], name="invert")
       result = wf.compute(inverted)

   print(result)
   #    inverted
   # 0  /.../bif_data/cache/v1/results/.../records/rec_.../assets/image1_inv.tif
   # 1  /.../bif_data/cache/v1/results/.../records/rec_.../assets/image2_inv.tif

What happens:

1. ``files(path=...)`` creates a graph node --- no computation yet.
2. ``invert(image=images["path"])`` binds the ``image`` input to the ``path``
   column of the source node.
3. ``wf.compute(inverted)`` executes the DAG in topological order.

The result is a pandas DataFrame with one column per output field.
Owned output assets are stored in immutable v1 records under
``./bif_data/cache/v1/results/.../<result-key>/records/<record-id>/`` and the
run view under ``./bif_data/runs/`` points back to the selected record.
:doc:`concepts/caching` covers the cache lifecycle, ``plan()`` and
``invalidate()``. Wetlands environments for this run are kept under
``./wetlands`` because the script called ``configure_wetlands()``.

Re-running is free
------------------

Run the same workflow again and it completes instantly --- the cache recognises
that inputs, parameters, and tool versions haven't changed:

.. code-block:: python

   with Workflow(storage_path="./bif_data", engine="wetlands") as wf:
       images = files(path="/data/raw", pattern="*.tif")
       inverted = invert(image=images["path"], name="invert")
       result = wf.compute(inverted)  # cache hit, no recomputation

Next steps
----------

- :doc:`concepts/graph` --- nodes, column references, branching, and source
  tools
- :doc:`tutorials/custom_tool` --- writing your own ProcessingTool and
  DataFrameTool
- :doc:`reference/environments` --- Wetlands paths, environment
  configuration, and per-environment runtime settings
- :doc:`concepts/architecture` --- understand the two-package design
