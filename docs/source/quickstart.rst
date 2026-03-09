Quick Start
===========

This guide walks you through building and running your first BioImageFlow
pipeline in under 5 minutes.

Define a source
---------------

A source tool produces the initial DataFrame that feeds the pipeline. Use a
:class:`~bioimageflow.DataFrameTool` to load file paths, read a CSV, or generate
data programmatically:

.. code-block:: python

   import pandas as pd
   from bioimageflow import DataFrameTool

   class ImageSource(DataFrameTool):
       name = "image_source"

       class Inputs:
           folder: str

       def transform(self, df, arguments):
           from pathlib import Path
           files = sorted(Path(arguments.folder).glob("*.tif"))
           return pd.DataFrame({"path": [str(f) for f in files]})

The ``Inputs`` class declares the parameters the tool expects. ``transform``
receives an empty DataFrame and returns the one you build.

Define a processing tool
------------------------

A :class:`~bioimageflow_core.ProcessingTool` runs on each row independently.
Declare its inputs, outputs, and the environment it needs:

.. code-block:: python

   from bioimageflow_core import (
       ProcessingTool, EnvironmentSpec, ImagePath, Arguments,
   )

   class InvertImage(ProcessingTool):
       name = "invert"
       environment = EnvironmentSpec(name="skimage", dependencies={})

       class Inputs:
           image: ImagePath()

       class Outputs:
           inverted: ImagePath() = "{image.stem}_inv.tif"

       def process_row(self, arguments: Arguments) -> "InvertImage.Outputs":
           from skimage.io import imread, imsave
           img = imread(arguments.image)
           out = 255 - img
           imsave(str(arguments.inverted), out)
           return self.Outputs(inverted=arguments.inverted)

Key points:

- ``Inputs`` fields that receive upstream data use type annotations like
  :func:`~bioimageflow_core.ImagePath`.
- ``Outputs`` fields have default values that are **output path templates**
  (see :doc:`tutorials/output_templating`).
- ``process_row`` receives an :class:`~bioimageflow_core.Arguments` object with
  resolved values for every input and output field.

Build the workflow
------------------

Wire tools together inside a :class:`~bioimageflow.Workflow` context manager:

.. code-block:: python

   from bioimageflow import Workflow

   source = ImageSource()
   invert = InvertImage()

   with Workflow(storage_path="./bif_data") as wf:
       images = source(folder="/data/raw")
       inverted = invert(image=images["path"])
       result = wf.compute(inverted)

   print(result)
   #    inverted
   # 0  bif_data/data/invert/abc123/assets/image1_inv.tif
   # 1  bif_data/data/invert/abc123/assets/image2_inv.tif

What happens:

1. ``source(folder=...)`` creates a graph node --- no computation yet.
2. ``invert(image=images["path"])`` binds the ``image`` input to the ``path``
   column of the source node.
3. ``wf.compute(inverted)`` executes the DAG in topological order.

The result is a pandas DataFrame with one column per output field.

Re-running is free
------------------

Run the same workflow again and it completes instantly --- the cache recognises
that inputs, parameters, and tool versions haven't changed:

.. code-block:: python

   with Workflow(storage_path="./bif_data") as wf:
       images = source(folder="/data/raw")
       inverted = invert(image=images["path"])
       result = wf.compute(inverted)  # cache hit, no recomputation

Next steps
----------

- :doc:`tutorials/basic_workflow` --- longer walkthrough with branching
- :doc:`tutorials/custom_tool` --- writing your own ProcessingTool and
  DataFrameTool
- :doc:`concepts/architecture` --- understand the two-package design
