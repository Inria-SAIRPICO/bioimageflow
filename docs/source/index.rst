BioImageFlow
=============

A Python library for orchestrating bioimage analysis workflows as reproducible
DAG pipelines.

BioImageFlow lets you declare image-processing tools, wire them into directed
acyclic graphs, and execute them with automatic caching, type checking, and
provenance tracking.

.. code-block:: python

   from pathlib import Path
   from typing import Annotated

   from bioimageflow_core import (
       ProcessingTool, EnvironmentSpec, ImageSpec, Arguments, Template,
   )
   from bioimageflow import Workflow, configure_wetlands
   from bioimageflow_common_tools import Files

   class Threshold(ProcessingTool):
       display_name = "Threshold"
       environment = EnvironmentSpec(name="base", dependencies={})

       class Inputs:
           image: Annotated[Path, ImageSpec()]
           cutoff: float = 128.0

       class Outputs:
           mask: Annotated[Path, ImageSpec(semantics={"binary"})] = Template(
               "{image.stem}_mask.tif"
           )

       def process_row(self, arguments: Arguments) -> "Threshold.Outputs":
           import numpy as np
           from skimage.io import imread, imsave

           img = imread(arguments.image)
           mask = (img > arguments.cutoff).astype(np.uint8) * 255
           imsave(str(arguments.mask), mask)
           return self.Outputs(mask=arguments.mask)

   threshold = Threshold()
   files = Files()

   configure_wetlands(wetlands_instance_path="./wetlands")

   with Workflow(storage_path="./bif_data") as wf:
       raw = files(path="/data/images", pattern="*.tif")
       masks = threshold(image=raw["path"], cutoff=100.0)
       result = wf.compute(masks)

Features
--------

- **DAG workflow engine** --- build pipelines by connecting tools, not writing glue code
- **Two-package architecture** --- a zero-dependency core for workers, a pandas/pydantic orchestrator for the main process
- **Typed image I/O** --- semantic, layout, and dtype constraints checked at graph-construction time
- **Automatic caching** --- signature-hash based caching skips redundant computation
- **Shared memory** --- zero-copy array transfer between tools
- **Merge strategies** --- inner join, cross join, concat, and collect
- **Output templating** --- declarative output path patterns
- **Environment isolation** --- each tool declares its own dependencies

.. toctree::
   :maxdepth: 2
   :caption: Workflow Authors

   installation
   quickstart
   concepts/index
   tutorials/index

.. toctree::
   :maxdepth: 2
   :caption: GUI / Platform Integrators

   gui/index

.. toctree::
   :maxdepth: 2
   :caption: Reference

   reference/index
   specs
