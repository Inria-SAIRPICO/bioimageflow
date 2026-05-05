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
       ProcessingTool, EnvironmentSpec, GUIMeta, Connectable, ImagePath, Arguments,
       Template,
   )

   class GaussianBlur(ProcessingTool):
       display_name = "Gaussian Blur"
       environment = EnvironmentSpec(name="skimage", dependencies={})

       class Inputs:
           image: ImagePath()
           sigma: Annotated[float, GUIMeta(min=0.1, max=50.0, step=0.1)] = 1.0

       class Outputs:
           blurred: ImagePath() = Template("{image.stem}_blur.tif")

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
- **Outputs**: path fields with ``Template(...)`` defaults are **output path templates**
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
           import numpy as np
           from skimage.io import imread

           # Load every image once, stack into one batch tensor.
           batch = np.stack([imread(args.image) for args in arguments_list])
           predictions = self.model.predict(batch)  # one forward pass

           # Emit one Outputs per Arguments — order must match arguments_list.
           return [
               self.Outputs(label=label, confidence=float(conf))
               for (label, conf) in predictions
           ]

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
           tile: ImagePath() = Template("{image.stem}_tile_{row_index}.tif")

       def process_row(self, arguments: Arguments) -> list:
           from skimage.io import imread, imsave

           img = imread(arguments.image)
           size = arguments.tile_size

           # arguments.tile points at the assets directory for this row;
           # write every child under its parent so explosion outputs land
           # in the cache, not the cwd.
           assets_dir = arguments.tile.parent
           stem = arguments.image.stem

           tiles = []
           for y in range(0, img.shape[0], size):
               for x in range(0, img.shape[1], size):
                   patch = img[y:y+size, x:x+size]
                   path = assets_dir / f"{stem}_tile_{len(tiles)}.tif"
                   imsave(str(path), patch)
                   tiles.append(self.Outputs(tile=path))
           return tiles

A 1-to-N tool resolves its own row's output template (``arguments.tile``)
to the canonical asset path; writing siblings under
``arguments.tile.parent`` keeps every emitted file inside that node's cache
directory (specs.md §7.1). The index expands from ``"0"`` to ``"0::0"``,
``"0::1"``, etc.

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
``connectable=Connectable.NOT_BY_DEFAULT`` with no display text or description.

``GUIMeta`` is supported on both ``Inputs`` and ``Outputs`` fields. Common
hints:

- ``display_name`` --- short human-readable label shown in the GUI.
- ``description`` --- longer tooltip / help text.
- ``connectable`` --- pin visibility for inputs (ignored on outputs).
- ``min`` / ``max`` / ``step`` --- numeric widget bounds.
- ``group`` --- tab / section name to organise related fields.

The ``connectable`` parameter is a :class:`~bioimageflow_core.Connectable` enum:

- ``Connectable.NEVER`` --- no pin, no toggle. For source/structural config.
- ``Connectable.NOT_BY_DEFAULT`` --- pin hidden, checkbox reveals it. For algorithm parameters.
- ``Connectable.BY_DEFAULT`` --- pin visible. For data inputs.

.. code-block:: python

   from typing import Annotated
   from bioimageflow_core import GUIMeta, Connectable, IOModel, Template

   class Inputs(IOModel):
       # Data input: BY_DEFAULT shows the pin; label + tooltip for the GUI
       image: Annotated[Path, GUIMeta(
           display_name="Input image",
           description="Raw intensity image to blur.",
           connectable=Connectable.BY_DEFAULT,
       )]

       # Algorithm parameter: uses the default (NOT_BY_DEFAULT)
       sigma: Annotated[float, GUIMeta(
           display_name="Sigma",
           description="Gaussian kernel standard deviation, in pixels.",
           min=0.1, max=50.0, step=0.1,
       )] = 1.0

       # This field can never be connected
       iterations: Annotated[int, GUIMeta(
           display_name="Iterations",
           description="Number of times to apply the blur.",
           connectable=Connectable.NEVER, min=1,
       )] = 3

   class Outputs(IOModel):
       blurred: Annotated[Path, GUIMeta(
           display_name="Blurred image",
           description="Gaussian-blurred output image.",
       )] = Template("{image.stem}_blur.tif")

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
