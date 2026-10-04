Tools
=====

BioImageFlow has two tool types with distinct process and declaration contracts.
Concrete ProcessingTools require an environment, IOModel outputs and explicit row-consumption semantics.
DataFrameTools require no worker environment or RowConsumption and may expose static, Passthrough or dynamic outputs without an Outputs declaration.

Process boundaries
------------------

Choose the tool type by process boundary first:

- Use :class:`~bioimageflow_core.ProcessingTool` for row-wise or batched work
  that should run in an isolated worker environment.
- Use :class:`~bioimageflow.DataFrameTool` for main-process dataframe
  loading, filtering, merging, and aggregation.

Tool-specific dependencies for ``ProcessingTool`` classes must be imported
inside ``process_row`` or ``process_batch``. Module import must stay light so
schemas can be inspected by the orchestrator, docs, tests, and GUIs without
installing every worker dependency. Imports from the Python standard library
and ``bioimageflow-core`` are safe at module level.

ProcessingTool
--------------

:class:`~bioimageflow_core.ProcessingTool` is the workhorse of BioImageFlow.
It runs in an isolated environment and processes data row-by-row or in batches.

**When to use:** image processing, segmentation, feature extraction,
measurement --- anything that operates on individual images or arrays.

.. code-block:: python

   from pathlib import Path
   from typing import Annotated

   from bioimageflow_core import (
       Arguments, GENERAL_ENV, IOModel, ImageSpec, ProcessingTool, RowConsumption, Template,
   )

   class MyTool(ProcessingTool):
       display_name = "My Tool"
       environment = GENERAL_ENV  # or a custom EnvironmentSpec for specialized deps
       row_consumption = RowConsumption.MAPPED

       class Inputs(IOModel):
           image: Annotated[Path, ImageSpec()]
           threshold: float = 0.5

       class Outputs(IOModel):
           mask: Annotated[Path, ImageSpec(semantics={"binary"})] = Template(
               "{image.stem}_mask.tif"
           )

       def process_row(self, arguments: Arguments) -> "MyTool.Outputs":
           from skimage.io import imread, imsave

           mask = imread(arguments.image) > arguments.threshold
           imsave(str(arguments.mask), mask.astype("uint8"))
           return self.Outputs(mask=arguments.mask)

Key properties:

- **Isolated execution**: each tool declares an
  :class:`~bioimageflow_core.EnvironmentSpec`. Use
  :data:`~bioimageflow_core.GENERAL_ENV` for tools that only need standard
  scientific packages (numpy, scipy, scikit-image, imageio, tifffile, Pillow)
  or the Python standard library.
  Simple download, path, CSV/table, and file utility tools should use
  ``GENERAL_ENV`` instead of declaring one-off environments.
  Tools with specialized dependencies declare their own ``EnvironmentSpec``.
- **Row-level parallelism**: ``process_row`` is called once per row, enabling
  future parallel execution.
- **Batch mode**: override ``process_batch`` for GPU-batched operations.
- **Row meaning**: ``RowConsumption.MAPPED`` means independent input rows; ``COLLECTIVE`` means whole-batch input semantics, independently of scheduling or output cardinality.
  Isolated collective inference, training and aggregation retain all-consumed-input association and aggregate lineage; they do not require repeated aggregate rows or relocation into a DataFrameTool.
  The exact correlated aggregate representation is a test/code-review obligation, not an additional mode introduced by this guide.
- **Explosion**: return a list from ``process_row`` to produce multiple output
  rows (e.g., tiling).

DataFrameTool
-------------

:class:`~bioimageflow.DataFrameTool` runs in the main process and transforms
entire DataFrames. It has access to pandas and pydantic.

**When to use:** loading data, filtering rows, reshaping tables, combining
results, computing aggregate statistics.

.. code-block:: python

   from bioimageflow import DataFrameTool
   from bioimageflow_core import IOModel

   class MyTransform(DataFrameTool):
       display_name = "My Transform"

       class Inputs(IOModel):
           min_area: float = 100.0

       def transform(self, df, arguments):
           return df[df["area"] >= arguments.min_area]

Key properties:

- **Main-process only**: has access to the full pandas DataFrame.
- **Merge control**: override ``merge_dataframes`` to customize how multiple
  upstream DataFrames are combined.
- **Passthrough**: use :class:`~bioimageflow.Passthrough` outputs to signal that input columns are preserved.
  Additional declared output fields retain their type, image and viewer metadata; a marker must not erase them.
- **Source tools**: set ``accepts_upstream = False`` on tools that produce a
  DataFrame from constants alone (e.g. ``Files``, ``Generate``). Constructing
  a source tool with positional upstream arguments raises
  :class:`~bioimageflow.SourceToolUpstreamError`. See
  :doc:`graph` for the source-node patterns.

Dynamic output schemas
~~~~~~~~~~~~~~~~~~~~~~

Most tools declare their output columns statically on the ``Outputs`` class.
Two cases need a *dynamic* schema — output column names that depend on
runtime values:

**Inputs-driven schema** — override ``resolve_outputs(cls, inputs)`` when
the column names come from constant parameters. ``Generate`` is the
canonical example, where ``column_name`` is a runtime parameter:

.. code-block:: python

   from bioimageflow_common_tools import Generate

   gen = Generate()
   sweep = gen(column_name="x", values=[0.1, 0.5, 1.0])

   sweep["x"]            # OK — column_name resolved at construction time
   sweep["unknown"]      # raises ColumnNotFoundError immediately

**Upstream-driven schema** — built-in merge tools (``InnerJoin``,
``CrossJoin``, ``JoinOnColumn``, ``Concat``, ``Collect``) override
``resolve_merge_schema(cls, upstream_schemas, inputs)`` instead, because
their output columns depend on the *upstream* schemas, not on their own
inputs.

A node's effective schema is available via :meth:`Node.get_output_schema`,
and is also what ``node["col"]`` consults for construction-time
validation.

IOModel
-------

:class:`~bioimageflow_core.IOModel` is the lightweight base class for
``Inputs`` and ``Outputs``. It's not pydantic --- it's a simple namespace with
type annotations and structural field checks.
It rejects missing required or unknown constructor fields; semantic type/value validation belongs to the orchestrator.
Resolved inherited/postponed annotations preserve metadata, and unavailable names are actionable declaration errors rather than invented unknown types.

.. code-block:: python

   class Inputs(IOModel):
       image: Annotated[Path, ImageSpec()]
       sigma: float = 1.0

- Fields without defaults are **required** (must be bound to upstream columns
  or constants).
- Fields with defaults may be omitted; nullability is independent.
  A nullable field with no default remains required, and explicitly supplied ``None`` is distinct from a missing value.
- Portable annotations describe graph validation and template resolution; schema display strings are not authoritative Python types.
  Numeric NumPy runtime values may cross current typed transport without allowing arbitrary third-party annotation classes.
- GUI bounds and picker choices are display hints, not validation of actual image pixels, biological meaning or filesystem existence.
- Attach :class:`~bioimageflow_core.GUIMeta` to provide GUI hints (see
  :doc:`type_system`).

Arguments
---------

:class:`~bioimageflow_core.Arguments` is the namespace passed to
``process_row`` and ``process_batch``. It contains resolved values for all
input and output fields:

.. code-block:: python

   def process_row(self, arguments: Arguments):
       arguments.image    # Path to the input image
       arguments.sigma    # float, resolved from constant or column
       arguments.mask     # Path, resolved from output template

If you access a non-existent attribute, ``Arguments`` raises an
``AttributeError`` with close-match suggestions (via ``difflib``).
