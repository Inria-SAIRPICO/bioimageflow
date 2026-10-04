Name Output Files
=================

When a :class:`~bioimageflow_core.ProcessingTool` produces file outputs, you
declare **output path templates** with the explicit
:class:`~bioimageflow_core.Template` marker on the ``Outputs`` class. The
engine resolves these templates before calling ``process_row``.

Basic templates
---------------

.. code-block:: python

   from bioimageflow_core import IOModel, RowConsumption

   # Partial tool sketch; provide process_row and the actual admitted runtime.
   class Segment(ProcessingTool):
       row_consumption = RowConsumption.MAPPED
       display_name = "Segment"
       environment = EnvironmentSpec(name="cellpose", dependencies={})

       class Inputs(IOModel):
           image: Annotated[Path, ImageSpec()]

       class Outputs(IOModel):
           mask: Annotated[Path, ImageSpec(semantics={"label"})] = Template(
               "{image.stem}_mask.tif"
           )

This is a base-name example, not a guarantee that duplicate input basenames cannot collide.
Accepted target intent rejects missing variables, invalid path accessors and owned-output collisions before affected dispatch; T/C assesses conformance.

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
     - Execution timestamp; not scientific identity or a guarantee of unique paths
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

Any input annotated with a path type, including
``Annotated[Path, ImageSpec(...)]``, exposes ``.stem``, ``.name``, ``.ext``,
and ``.exts``:

.. code-block:: python

   class Outputs(IOModel):
       # Given image = "cells.ome.tif"
       result: Annotated[Path, ImageSpec()] = Template(
           "{image.stem}_result{image.exts}"
       )
       # → "cells.ome_result.ome.tif"

Column references
-----------------

Access values from the upstream DataFrame with ``{column:<name>}``:

.. code-block:: python

   class Outputs(IOModel):
       report: Path = Template("{column:sample_id}_report.csv")

Row index
---------

``{row_index}`` identifies the admitted input row when the engine resolves a base path.
A one-to-many tool generates distinct child paths beneath that base path's owned asset directory; exploded output indexes are not available before execution:

.. code-block:: python

   class TileImage(ProcessingTool):
       row_consumption = RowConsumption.MAPPED
       display_name = "Tile"
       # ...

       class Outputs(IOModel):
           tile: Annotated[Path, ImageSpec()] = Template(
               "{image.stem}_tile_{row_index}.tif"
           )
           # Base path for input row 0: "cell_001_tile_0.tif".
           # process_row derives unique child filenames before writing.

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

Ownership and refusal
----------------------

Bare declared scalar input values may interpolate without path accessors; ``.name/.stem/.ext/.exts`` require a path input.
Unknown fields/columns and unresolved expressions refuse instead of being written as literal placeholder text.
Tool-returned paths must identify admitted owned assets or explicit external values; string or URL spelling alone does not confer ownership.
Distinct output fields, rows, runs and child outputs need collision-safe owned destinations.
Accepted collective and empty-input aggregate artifacts retain explicit input association without fabricated object rows.
