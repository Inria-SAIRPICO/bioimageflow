Basic Workflow
==============

This tutorial builds a simple linear pipeline: load images, segment them,
then measure properties of the segmented regions.

Before running
--------------

BioImageFlow writes to a few distinct locations:

- The workflow ``storage_path`` contains node outputs, cached
  DataFrames, metadata, and generated assets.
- The Wetlands instance path contains isolated tool environments,
  Wetlands logs, debug metadata, and the bundled Pixi or Micromamba
  installation.
- The tool store contains versioned tool packages installed by
  ``require_tool_packages()`` or ``Workflow.load()``. It is resolved
  from ``BIOIMAGEFLOW_TOOL_STORE``, then
  ``BIOIMAGEFLOW_HOME / "tool_packages"``, then
  ``~/.bioimageflow/tool_packages``.

Use explicit project-local paths while learning:

.. code-block:: python

   from bioimageflow import Workflow, configure_wetlands

   configure_wetlands(wetlands_instance_path="./wetlands")

   with Workflow(storage_path="./bif_data") as wf:
       ...

``configure_wetlands()`` must run before Wetlands is first initialized:
that means before ``Workflow.compute()``, ``Workflow.load()``, or
``require_tool_packages()``. If it is omitted, BioImageFlow uses
``BIOIMAGEFLOW_WETLANDS``, then ``BIOIMAGEFLOW_HOME / "wetlands"``,
then ``~/.bioimageflow/wetlands``.

Setting up tools
----------------

First, define the tools. We'll use stub implementations to focus on the
workflow mechanics:

.. code-block:: python

   import pandas as pd
   from pathlib import Path
   from bioimageflow_core import (
       ProcessingTool, EnvironmentSpec, ImagePath, Arguments,
   )
   from bioimageflow import DataFrameTool

   class LoadImages(DataFrameTool):
       """Scan a folder for TIFF files."""
       display_name = "Load Images"

       class Inputs:
           folder: str

       def transform(self, df, arguments):
           files = sorted(Path(arguments.folder).glob("*.tif"))
           return pd.DataFrame({"image": [str(f) for f in files]})


   cellpose_env = EnvironmentSpec(
       name="cellpose",
       dependencies={"conda": ["cellpose==4.0.8"], "python": "3.12"},
   )

   class Segment(ProcessingTool):
       """Segment cells using Cellpose."""
       display_name = "Segment"
       environment = cellpose_env

       class Inputs:
           image: ImagePath()

       class Outputs:
           mask: ImagePath(semantics={"label"}) = "{image.stem}_seg.tif"

       def process_row(self, arguments: Arguments) -> "Segment.Outputs":
           # In a real tool, you would call Cellpose here
           from skimage.io import imread, imsave
           import numpy as np

           img = imread(arguments.image)
           mask = np.zeros_like(img, dtype=np.int32)
           imsave(str(arguments.mask), mask)
           return self.Outputs(mask=arguments.mask)


   class Measure(ProcessingTool):
       """Measure region properties from a label mask."""
       display_name = "Measure"
       environment = EnvironmentSpec(name="skimage", dependencies={})

       class Inputs:
           mask: ImagePath(semantics={"label"})

       class Outputs:
           cell_count: int
           mean_area: float

       def process_row(self, arguments: Arguments) -> "Measure.Outputs":
           from skimage.io import imread
           from skimage.measure import regionprops

           mask = imread(arguments.mask)
           props = regionprops(mask)
           count = len(props)
           area = sum(p.area for p in props) / max(count, 1)
           return self.Outputs(cell_count=count, mean_area=area)

Building the DAG
----------------

Wire the tools together in a :class:`~bioimageflow.Workflow`:

.. code-block:: python

   from bioimageflow import Workflow, configure_wetlands

   loader = LoadImages()
   segment = Segment()
   measure = Measure()

   configure_wetlands(wetlands_instance_path="./wetlands")

   with Workflow(storage_path="./bif_data") as wf:
       raw = loader(folder="/data/experiment_01")
       masks = segment(image=raw["image"])
       stats = measure(mask=masks["mask"])
       result = wf.compute(stats)

   print(result)
   #    cell_count  mean_area
   # 0          42     156.3
   # 1          38     162.1

The pipeline forms a linear chain:

.. code-block:: text

   LoadImages  -->  Segment  -->  Measure

Each arrow represents a column binding --- ``raw["image"]`` feeds into the
``image`` input of ``Segment``, and ``masks["mask"]`` feeds into ``Measure``.

During execution, generated masks are written under
``./bif_data/data/segment/<timestamp>_<hash>/assets/``. The Cellpose
environment for ``Segment`` is created under ``./wetlands``.

Computing multiple targets
--------------------------

You can request results from any node, not just the last one. Pass multiple
targets to get a dictionary:

.. code-block:: python

   with Workflow(storage_path="./bif_data") as wf:
       raw = loader(folder="/data/experiment_01")
       masks = segment(image=raw["image"])
       stats = measure(mask=masks["mask"])

       results = wf.compute(masks, stats)
       # results is a dict: {"segment": DataFrame, "measure": DataFrame}

Progress monitoring
-------------------

Track execution progress with a callback:

.. code-block:: python

   from bioimageflow import Workflow, ProgressEvent

   def on_progress(event: ProgressEvent):
       if event.status == "started":
           print(f"Starting {event.node_name}...")
       elif event.status == "row_complete":
           print(f"  {event.node_name}: {event.row}/{event.total_rows}")
       elif event.status == "completed":
           print(f"Done: {event.node_name}")

   with Workflow(storage_path="./bif_data", on_progress=on_progress) as wf:
       raw = loader(folder="/data/experiment_01")
       masks = segment(image=raw["image"])
       result = wf.compute(masks)

Next steps
----------

- :doc:`/concepts/graph` --- nodes, column references, branching, source tools
- :doc:`custom_tool` --- write your own tools from scratch
- :doc:`/reference/environments` --- Wetlands paths and environment settings
