Output Templating
=================

When a :class:`~bioimageflow_core.ProcessingTool` produces file outputs, you
declare **output path templates** with the explicit
:class:`~bioimageflow_core.Template` marker on the ``Outputs`` class. The
engine resolves these templates before calling ``process_row``.

Basic templates
---------------

.. code-block:: python

   class Segment(ProcessingTool):
       display_name = "Segment"
       environment = EnvironmentSpec(name="cellpose", dependencies={})

       class Inputs:
           image: ImagePath()

       class Outputs:
           mask: ImagePath(semantics={"label"}) = Template("{image.stem}_mask.tif")

The template ``{image.stem}_mask.tif`` resolves using the ``image`` input path:

- Input: ``/data/experiment/cell_001.tif``
- Output: ``<assets_dir>/cell_001_mask.tif``

Available variables
-------------------

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Variable
     - Description
   * - ``{node_name}``
     - Name of the current node
   * - ``{row_index}``
     - Current row index (string)
   * - ``{timestamp}``
     - Unix timestamp of execution
   * - ``{<input>.stem}``
     - Stem of an input path (filename without extension)
   * - ``{<input>.name}``
     - Full filename of an input path
   * - ``{<input>.ext}``
     - Extension of an input path (e.g., ``.tif``)
   * - ``{<input>.exts}``
     - All extensions (e.g., ``.ome.tif``)
   * - ``{<input>}``
     - Input value, useful for scalar parameters such as channel indices
   * - ``{ext}``
     - Extension from the single path input (shorthand)
   * - ``{column:<name>}``
     - Value of a column from the upstream DataFrame

Path-derived variables
----------------------

Any input annotated with a path type (:func:`~bioimageflow_core.ImagePath`)
exposes ``.stem``, ``.name``, ``.ext``, and ``.exts``:

.. code-block:: python

   class Outputs:
       # Given image = "cells.ome.tif"
       result: ImagePath() = Template("{image.stem}_result{image.exts}")
       # → "cells_result.ome.tif"

Column references
-----------------

Access values from the upstream DataFrame with ``{column:<name>}``:

.. code-block:: python

   class Outputs:
       report: Path = Template("{column:sample_id}_report.csv")

Row index
---------

``{row_index}`` is especially useful for one-to-many (explosion) tools where
a single input row produces multiple outputs:

.. code-block:: python

   class TileImage(ProcessingTool):
       display_name = "Tile"
       # ...

       class Outputs:
           tile: ImagePath() = Template("{image.stem}_tile_{row_index}.tif")
           # → "cell_001_tile_0::0.tif", "cell_001_tile_0::1.tif", ...

Resolution order
----------------

Templates are resolved by the engine *before* ``process_row`` is called. The
resolved path is passed to ``process_row`` via the
:class:`~bioimageflow_core.Arguments` object. The tool writes its output to
this path and returns it in the ``Outputs``.

.. code-block:: python

   def process_row(self, arguments: Arguments) -> "Segment.Outputs":
       # arguments.mask is already a resolved Path
       imsave(str(arguments.mask), mask_array)
       return self.Outputs(mask=arguments.mask)
