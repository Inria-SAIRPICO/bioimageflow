Writing Custom Tools
====================

BioImageFlow has two kinds of tools. This guide shows how to write each.

Workflow-local custom tools
---------------------------

A workflow can carry its own custom tools beside the workflow script. Use
this for tools that belong to one workflow or one project and are not
intended as a reusable, versioned tool package.

Recommended layout:

.. code-block:: text

   fish_analysis/
     workflow.py
     tools/
       download_images.py
       average_spots_per_nucleus.py
       utils.py
       data/
         small_runtime_asset.json
     tests/
       test_tools.py
       data/
         small_input.csv

Import those tools from the workflow script and use them like package
tools:

.. code-block:: python

   from bioimageflow import Workflow
   from tools.average_spots_per_nucleus import AverageSpotsPerNucleus
   from tools.download_images import DownloadImages

   with Workflow(storage_path="./results") as wf:
       images = DownloadImages()(dataset="demo")
       stats = AverageSpotsPerNucleus()(images)
       wf.export("workflow.json")

``Workflow.export(path)`` embeds the workflow-local ``tools/`` package in
the exported JSON when custom tools from that package are used.
``Workflow.load(path, storage_path=results)`` reconstructs the package before falling back to
imports, so the exported workflow can move to another machine without
separately installing the custom tool code.

Keep workflow-local tools small and explicit:

- Put tool classes in ``tools/*.py`` and import them from their concrete modules in ``workflow.py``, for example ``from tools.download_images import DownloadImages``.
- The ``tools/`` directory may omit ``__init__.py`` on supported Python versions.
  If you add ``tools/__init__.py`` as a regular package marker, keep it empty or limited to a docstring.
  Do not use eager barrel exports such as ``from .some_tool import SomeTool`` there, because workers import one tool module inside that tool's isolated environment and should not need dependencies for unrelated tools.
- Put helper modules, package constants, and small runtime assets needed
  by the custom tools under ``tools/``. Use relative imports inside that
  package, for example ``from .utils import normalize``.
- Put tests under ``tests/`` next to the workflow. Test schema
  serialization, parameter validation, and at least one execution path
  with tiny data.
- Store tiny committed fixtures under ``tests/data/``. Generated test
  output belongs in pytest temporary directories or the workflow
  ``storage_path`` and should not be committed.
- Ship small static assets under ``tools/data/`` only when they are part
  of the tool behavior. Large models, binaries, downloaded datasets, or
  generated artifacts should be downloaded, declared as
  environment/package dependencies, or produced at runtime.
- Add tests for every workflow-local tool before relying on it in a
  workflow. At minimum, cover input validation, output schema, one tiny
  successful execution, and expected failures for invalid parameters or
  missing files.

Authoring process
^^^^^^^^^^^^^^^^^

Use a test-first loop for each new tool:

1. Write a failing test that describes the smallest useful behavior.
2. Define ``Inputs`` and ``Outputs`` with serializable standard-library
   types and BioImageFlow metadata only.
3. Implement the simplest execution path using tiny committed fixtures or
   synthetic arrays.
4. Add failure tests for invalid inputs, missing dependencies, and
   unsupported image types.
5. Run the tool inside a one-node ``Workflow`` so cache paths, output
   templates, and returned ``Outputs`` are exercised.

Tool code must respect the process boundary. A ``ProcessingTool`` runs in
its declared worker environment; put heavy or tool-specific imports inside
``process_row`` or ``process_batch``. Module-level imports are limited to
the Python standard library and ``bioimageflow-core``. Use
``DataFrameTool`` only for main-process table operations that need pandas
or whole-dataframe context.

ProcessingTool
--------------

A :class:`~bioimageflow_core.ProcessingTool` processes rows independently and
runs inside an isolated environment. This is the primary tool type for image
analysis operations.

Minimal example
^^^^^^^^^^^^^^^

.. code-block:: python

   from pathlib import Path
   from typing import Annotated
   from bioimageflow_core import (
       ProcessingTool, GENERAL_ENV, GUIMeta, Connectable, ImageSpec, Arguments,
       Template,
   )

   class GaussianBlur(ProcessingTool):
       display_name = "Gaussian Blur"
       environment = GENERAL_ENV

       class Inputs:
           image: Annotated[Path, ImageSpec()]
           sigma: Annotated[float, GUIMeta(min=0.1, max=50.0, step=0.1)] = 1.0

       class Outputs:
           blurred: Annotated[Path, ImageSpec()] = Template("{image.stem}_blur.tif")

       def process_row(self, arguments: Arguments) -> "GaussianBlur.Outputs":
           from skimage.io import imread, imsave
           from skimage.filters import gaussian

           img = imread(arguments.image)
           out = gaussian(img, sigma=arguments.sigma)
           imsave(str(arguments.blurred), out)
           return self.Outputs(blurred=arguments.blurred)

The ``skimage`` imports are inside ``process_row`` because they belong to
the worker environment declared by the tool.
``GENERAL_ENV`` already includes numpy, scipy, scikit-image, imageio, tifffile, and Pillow, so ordinary scientific image processing, downloading with the Python standard library, and simple file utilities should use it instead of declaring one-off environments.
This keeps schema discovery,
documentation generation, workflow loading, and GUI inspection from failing
when the worker-only dependency is not installed in the main process.

Anatomy:

- **display_name**: human-readable label for UIs and documentation. Runtime progress events use scoped node names; cache identity uses result keys.
- **environment**: declares the conda/pip dependencies this tool needs.
  Use ``GENERAL_ENV`` for tools whose runtime dependencies are covered by the general scientific stack.
  Declare a custom ``EnvironmentSpec`` only for specialized dependencies such as Cellpose, StarDist, SimpleITK, BioIO, command-line tools, model runtimes, or other packages not in ``GENERAL_ENV``.
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
       environment = GENERAL_ENV

       class Inputs:
           image: Annotated[Path, ImageSpec()]

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
           image: Annotated[Path, ImageSpec()]

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
       environment = GENERAL_ENV

       class Inputs:
           image: Annotated[Path, ImageSpec()]
           tile_size: int = 256

       class Outputs:
           tile: Annotated[Path, ImageSpec()] = Template(
               "{image.stem}_tile_{row_index}.tif"
           )

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

   with Workflow(storage_path="./results") as wf:
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

   importbio_env = EnvironmentSpec(
       name="importbio",
       dependencies={"pip": ["importbio>=1.0"]},
   )

   class ToolA(ProcessingTool):
       display_name = "Tool A"
       environment = importbio_env
       # ...

   class ToolB(ProcessingTool):
       display_name = "Tool B"
       environment = importbio_env
       # ...

The framework validates that all tools sharing an environment name declare
identical dependencies. Mismatches raise
:class:`~bioimageflow_core.EnvironmentMismatchError`.

Documentation and images
------------------------

Every reusable tool or workflow must have author-facing documentation and
tests in the same change. Keep examples runnable with the current package
layout and current public APIs; do not document private import paths.

Use images sparingly and commit them under ``docs/images/`` when they are
needed to explain a workflow, UI state, or expected output. Prefer small
PNG or WebP files generated from public-domain, permissively licensed, or
synthetic data. Do not commit private microscopy data, large raw images,
generated cache directories, or screenshots that cannot be regenerated.
