BioImageFlow
=============

A Python library for orchestrating bioimage analysis workflows as reproducible
DAG pipelines.

BioImageFlow lets you declare image-processing tools, wire them into directed
acyclic graphs, and execute them with automatic caching, type checking, and
provenance tracking.

Contributor guidance for reusable tool packages, workflow examples, and
review checklists lives in :doc:`reference/tool_package_strategy` and
:doc:`reference/agent_tool_workflow_playbook`.

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
       environment = EnvironmentSpec(
           name="imageio",
           dependencies={"python": "3.10", "pip": ["imageio", "numpy"]},
       )

       class Inputs:
           image: Annotated[Path, ImageSpec()]
           cutoff: float = 128.0

       class Outputs:
           mask: Annotated[Path, ImageSpec(semantics={"binary"})] = Template(
               "{image.stem}_mask.tif"
           )

       def process_row(self, arguments: Arguments) -> "Threshold.Outputs":
           import imageio.v3 as iio
           import numpy as np

           img = iio.imread(arguments.image)
           mask = (img > arguments.cutoff).astype(np.uint8) * 255
           iio.imwrite(arguments.mask, mask)
           return self.Outputs(mask=arguments.mask)

   threshold = Threshold()
   files = Files()

   configure_wetlands(wetlands_instance_path="./wetlands")

   with Workflow(storage_path="./bif_data", engine="wetlands") as wf:
       raw = files(path="/data/images", pattern="*.tif")
       masks = threshold(image=raw["path"], cutoff=100.0, name="threshold")
       result = wf.compute(masks)

Features
--------

- **DAG workflow engine** --- build pipelines by connecting tools, not writing glue code
- **Two-package architecture** --- a minimal worker-safe core, a pandas/pydantic orchestrator for the main process
- **Typed image I/O** --- semantic, layout, and dtype constraints checked at graph-construction time
- **Automatic caching** --- result-key/current-record caching skips redundant computation
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
   priority_workflows/index
   specialized_tool_workflows/index

.. toctree::
   :maxdepth: 2
   :caption: GUI / Platform Integrators

   gui/index

.. toctree::
   :maxdepth: 2
   :caption: Reference

   reference/index
   specs
