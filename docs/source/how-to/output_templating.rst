Name Output Files
=================

When a :class:`~bioimageflow_core.ProcessingTool` produces file outputs, you
declare **output path templates** with the explicit
:class:`~bioimageflow_core.Template` marker on the ``Outputs`` class. The
engine resolves exact destinations and checks their owners before calling the
affected row or batch method.
Bare ``Path`` outputs are returned references; they do not receive an implicit
output template.

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

This declaration owns one exact file per mapped input row.
If repeated inputs or equal basenames resolve to the same destination, the
engine raises an explained collision before this node writes anything.
Include ``{row_index}`` or another distinguishing value when needed.

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
   * - ``{group_index}``
     - Filename-safe ordinal shared by rows contributing to one file
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

``{row_index}`` identifies the admitted input row; it does not identify a
collective file group or an output row generated later.
Use ``PathBase`` when a tool derives a family of child filenames:

.. code-block:: python

   class TileImage(ProcessingTool):
       row_consumption = RowConsumption.MAPPED
       display_name = "Tile"
       # ...

       class Outputs(IOModel):
           tile: Annotated[Path, ImageSpec()] = PathBase(
               "{image.stem}_tile_{row_index}.tif"
           )
           # Base path for input row 0: "cell_001_tile_0.tif".
           # process_row derives unique child filenames before writing.

An exact ``Template`` can be returned in many records that reference the same
file, but it must not be used as an implicit naming base for different files.
``PathBase`` makes that distinction explicit and does not promise complete
collision detection for filenames generated inside the tool.

File groups
-----------

Tool authors declare which rows contribute to each exact file.
Workflow users edit the filename pattern, while this grouping stays fixed:

.. code-block:: python

   from bioimageflow_core import FileGroup, Template

   # Mapped tool: one file per input row (the mapped default).
   mask = Template("{image.stem}_mask.tif", group=FileGroup.per_row())

   # Mosaic: all tiles contribute to one file.
   mosaic = Template("mosaic.png", group=FileGroup.once())

   # TracksToLabels: one file for each full source-image path.
   tracks = Template(
       "{label_image.stem}_tracks.tif",
       group=FileGroup.by("label_image"),
   )

Collective tools specify the group explicitly.
Several input fields may form a key, for example ``FileGroup.by("image", "channel")``.
Two source images in different folders remain distinct groups even when their
stems are equal.
Members of one group must request the same path; different file owners,
including different output fields, must request different paths.

Use ``{group_index}`` to distinguish keyed outputs without giving different
names to rows within one group:

.. code-block:: text

   {label_image.stem}_tracks_{group_index}.tif

The index is deterministic for the same ordered inputs and is not a permanent
identifier across changed selections.
Adding ``{row_index}`` to a grouped output can be wrong: rows contributing to
one file would request different paths.

Grouping describes files, independently of ``RowConsumption`` and the number
of returned records.
A whole-node ``process_batch`` can use shared groups under mapped or collective
computation; a ``process_row`` callback supports per-row files only.
Batch callbacks can iterate ``context.output_files[field]`` to obtain each
admitted destination, its key values and contributing row/reference positions.

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
Different exact file owners need distinct destinations; repeated references
within one file group are intentional.
Accepted collective and empty-input aggregate artifacts retain explicit input association without fabricated object rows.

``OutputPathCollisionError`` explains when different owners target one path.
``OutputGroupDestinationMismatchError`` explains when one group requests
different paths.
Both expose structured template-expansion examples and bounded evidence for
checking a proposed correction.
Update the output template and compute again; valid unchanged cached results
can be reused.
Files are never silently renamed by the planner.
