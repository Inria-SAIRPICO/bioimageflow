Writing Custom Tools
====================

BioImageFlow has two kinds of tools. This tutorial shows how to write each.

ProcessingTool
--------------

A :class:`~bioimageflow_core.ProcessingTool` processes rows independently and
runs inside an isolated environment. This is the primary tool type for image
analysis operations.

Minimal example
^^^^^^^^^^^^^^^

.. code-block:: python

   from typing import Annotated
   from bioimageflow_core import (
       ProcessingTool, EnvironmentSpec, GUIMeta, ImagePath, Arguments,
   )

   class GaussianBlur(ProcessingTool):
       display_name = "Gaussian Blur"
       environment = EnvironmentSpec(name="skimage", dependencies={})

       class Inputs:
           image: ImagePath()
           sigma: Annotated[float, GUIMeta(connectable=False, min=0.1, max=50.0, step=0.1)] = 1.0

       class Outputs:
           blurred: ImagePath() = "{image.stem}_blur.tif"

       def process_row(self, arguments: Arguments) -> "GaussianBlur.Outputs":
           from skimage.io import imread, imsave
           from skimage.filters import gaussian

           img = imread(arguments.image)
           out = gaussian(img, sigma=arguments.sigma)
           imsave(str(arguments.blurred), out)
           return self.Outputs(blurred=arguments.blurred)

Anatomy:

- **display_name**: human-readable name used in cache paths and progress events.
- **environment**: declares the conda/pip dependencies this tool needs.
- **Inputs**: fields annotated with types. Fields with defaults are optional
  parameters; fields without defaults are required bindings.
- **Outputs**: fields with default strings are **output path templates**
  resolved by the engine before ``process_row`` is called.
- **process_row**: receives an :class:`~bioimageflow_core.Arguments` namespace
  with all resolved input and output values. Returns an ``Outputs`` instance.

Scalar outputs
^^^^^^^^^^^^^^

Outputs aren't limited to file paths. Return scalars for measurements:

.. code-block:: python

   class MeasureIntensity(ProcessingTool):
       display_name = "Measure Intensity"
       environment = EnvironmentSpec(name="skimage", dependencies={})

       class Inputs:
           image: ImagePath()

       class Outputs:
           mean: float
           std: float

       def process_row(self, arguments: Arguments) -> "MeasureIntensity.Outputs":
           from skimage.io import imread
           import numpy as np

           img = imread(arguments.image)
           return self.Outputs(mean=float(np.mean(img)), std=float(np.std(img)))

Batch processing
^^^^^^^^^^^^^^^^

Override ``process_batch`` instead of ``process_row`` when you need to process
all rows at once (e.g., for GPU batching):

.. code-block:: python

   class BatchClassifier(ProcessingTool):
       display_name = "Batch Classifier"
       environment = EnvironmentSpec(name="torch", dependencies={})

       class Inputs:
           image: ImagePath()

       class Outputs:
           label: str
           confidence: float

       def process_batch(self, arguments_list):
           results = []
           # Load all images, run model in batch, etc.
           for args in arguments_list:
               results.append(self.Outputs(label="cell", confidence=0.95))
           return results

One-to-many (explosion)
^^^^^^^^^^^^^^^^^^^^^^^

Return a **list** from ``process_row`` to produce multiple output rows from a
single input row. This is useful for tiling or splitting:

.. code-block:: python

   class TileImage(ProcessingTool):
       display_name = "Tile"
       environment = EnvironmentSpec(name="skimage", dependencies={})

       class Inputs:
           image: ImagePath()
           tile_size: int = 256

       class Outputs:
           tile: ImagePath() = "{image.stem}_tile_{row_index}.tif"

       def process_row(self, arguments: Arguments) -> list:
           from skimage.io import imread, imsave
           import numpy as np

           img = imread(arguments.image)
           size = arguments.tile_size
           tiles = []
           for y in range(0, img.shape[0], size):
               for x in range(0, img.shape[1], size):
                   patch = img[y:y+size, x:x+size]
                   path = f"{arguments.image.stem}_tile_{len(tiles)}.tif"
                   imsave(path, patch)
                   tiles.append(self.Outputs(tile=path))
           return tiles

The index expands from ``"0"`` to ``"0::0"``, ``"0::1"``, etc.

DataFrameTool
-------------

A :class:`~bioimageflow.DataFrameTool` transforms entire DataFrames in the main
process. Use it for loading data, filtering, reshaping, or any operation that
needs pandas.

Source tool (no upstream)
^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

   import pandas as pd
   from bioimageflow import DataFrameTool

   class CSVSource(DataFrameTool):
       display_name = "Csv Source"

       class Inputs:
           path: str

       def transform(self, df, arguments):
           return pd.read_csv(arguments.path)

Transform tool (with upstream)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

   class FilterByArea(DataFrameTool):
       display_name = "Filter By Area"

       class Inputs:
           min_area: float = 100.0

       def transform(self, df, arguments):
           return df[df["area"] >= arguments.min_area].reset_index(drop=True)

Use it in a pipeline:

.. code-block:: python

   measure = MeasureRegions()
   filter_tool = FilterByArea()

   with Workflow() as wf:
       stats = measure(mask=masks["mask"])
       filtered = filter_tool(stats, min_area=200.0)
       result = wf.compute(filtered)

DataFrameTools receive upstream nodes as positional arguments (``filter_tool(stats, ...)``).

Multiple upstream DataFrames
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Override ``merge_dataframes`` to control how multiple upstream DataFrames are
combined before ``transform`` is called:

.. code-block:: python

   class CombineResults(DataFrameTool):
       display_name = "Combine"

       def merge_dataframes(self, dfs, arguments):
           import pandas as pd
           return pd.concat(dfs, ignore_index=True)

       def transform(self, df, arguments):
           return df.sort_values("score", ascending=False)

Passthrough outputs
^^^^^^^^^^^^^^^^^^^

Use :class:`~bioimageflow.Passthrough` to indicate that a DataFrameTool
preserves input columns in its output:

.. code-block:: python

   from bioimageflow import DataFrameTool, Passthrough

   class AddColumn(DataFrameTool):
       display_name = "Add Column"

       class Outputs(Passthrough):
           new_col: str

       def transform(self, df, arguments):
           df["new_col"] = "hello"
           return df

With ``Passthrough``, downstream tools can reference both original columns and
new ones.

GUI metadata
------------

Use :class:`~bioimageflow_core.GUIMeta` to attach rendering hints that a GUI
can introspect. This is opt-in --- fields without ``GUIMeta`` default to
``connectable=True``.

.. code-block:: python

   from typing import Annotated
   from bioimageflow_core import GUIMeta, IOModel

   class Inputs(IOModel):
       # This field appears as a connectable port (default)
       image: ImagePath()

       # This field appears as a slider, not a connectable port
       sigma: Annotated[float, GUIMeta(connectable=False, min=0.1, max=50.0, step=0.1)] = 1.0

       # This field has min-only constraint
       iterations: Annotated[int, GUIMeta(connectable=False, min=1)] = 3

To introspect a tool's schema programmatically:

.. code-block:: python

   from bioimageflow import get_inputs_schema

   schema = get_inputs_schema(my_tool)
   for name, info in schema.items():
       print(f"{name}: connectable={info['connectable']}, type={info['type']}")

Environment sharing
-------------------

Multiple tools can share the same environment:

.. code-block:: python

   skimage_env = EnvironmentSpec(
       name="skimage",
       dependencies={"pip": ["scikit-image>=0.22"]},
   )

   class ToolA(ProcessingTool):
       display_name = "Tool A"
       environment = skimage_env
       # ...

   class ToolB(ProcessingTool):
       display_name = "Tool B"
       environment = skimage_env
       # ...

The framework validates that all tools sharing an environment name declare
identical dependencies. Mismatches raise
:class:`~bioimageflow_core.EnvironmentMismatchError`.
