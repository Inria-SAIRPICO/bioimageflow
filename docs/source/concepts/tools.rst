Tools
=====

BioImageFlow has two tool types, each suited to different kinds of operations.

ProcessingTool
--------------

:class:`~bioimageflow_core.ProcessingTool` is the workhorse of BioImageFlow.
It runs in an isolated environment and processes data row-by-row or in batches.

**When to use:** image processing, segmentation, feature extraction,
measurement --- anything that operates on individual images or arrays.

.. code-block:: python

   from bioimageflow_core import GENERAL_ENV, Template

   class MyTool(ProcessingTool):
       display_name = "My Tool"
       environment = GENERAL_ENV  # or a custom EnvironmentSpec for specialized deps

       class Inputs:
           image: ImagePath()
           threshold: float = 0.5

       class Outputs:
           mask: ImagePath(semantics={"binary"}) = Template("{image.stem}_mask.tif")

       def process_row(self, arguments: Arguments) -> "MyTool.Outputs":
           ...

Key properties:

- **Isolated execution**: each tool declares an
  :class:`~bioimageflow_core.EnvironmentSpec`. Use
  :data:`~bioimageflow_core.GENERAL_ENV` for tools that only need standard
  scientific packages (numpy, scipy, scikit-image, imageio, tifffile, Pillow).
  Tools with specialized dependencies declare their own ``EnvironmentSpec``.
- **Row-level parallelism**: ``process_row`` is called once per row, enabling
  future parallel execution.
- **Batch mode**: override ``process_batch`` for GPU-batched operations.
- **Explosion**: return a list from ``process_row`` to produce multiple output
  rows (e.g., tiling).

DataFrameTool
-------------

:class:`~bioimageflow.DataFrameTool` runs in the main process and transforms
entire DataFrames. It has access to pandas and pydantic.

**When to use:** loading data, filtering rows, reshaping tables, combining
results, computing aggregate statistics.

.. code-block:: python

   class MyTransform(DataFrameTool):
       display_name = "My Transform"

       class Inputs:
           min_area: float = 100.0

       def transform(self, df, arguments):
           return df[df["area"] >= arguments.min_area]

Key properties:

- **Main-process only**: has access to the full pandas DataFrame.
- **Merge control**: override ``merge_dataframes`` to customize how multiple
  upstream DataFrames are combined.
- **Passthrough**: use :class:`~bioimageflow.Passthrough` outputs to signal
  that input columns are preserved.
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
type annotations and validation.

.. code-block:: python

   class Inputs(IOModel):
       image: ImagePath()
       sigma: float = 1.0

- Fields without defaults are **required** (must be bound to upstream columns
  or constants).
- Fields with defaults are **optional parameters**.
- Annotations are used for type checking and template resolution.
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
