Graph Construction
==================

BioImageFlow pipelines are directed acyclic graphs (DAGs) built lazily by
calling tool instances. No computation happens until
:meth:`~bioimageflow.Workflow.compute` is called.

Nodes
-----

Every tool call creates a :class:`~bioimageflow.Node`:

.. code-block:: python

   segment = Segment()  # tool instance

   with Workflow() as wf:
       masks = segment(image=raw["image"])
       # masks is a Node, not a DataFrame

Nodes store:

- The tool that will execute
- Upstream node references
- Column bindings (which column from which upstream node)
- Constant bindings (literal values)

Column references
-----------------

Use ``node["column"]`` to create a :class:`~bioimageflow.ColumnRef`:

.. code-block:: python

   raw = loader(folder="/data")
   masks = segment(image=raw["image"])  # raw["image"] is a ColumnRef

This tells the engine: "when executing ``segment``, resolve its ``image``
input from the ``image`` column of ``raw``'s output DataFrame."

The framework validates at construction time that:

- The referenced column exists in the upstream node's outputs
- The type annotations are compatible (via
  :func:`~bioimageflow_core.check_compatibility`)

Binding rules
-------------

Tool inputs can be bound in three ways:

**Column reference** --- from an upstream node's output:

.. code-block:: python

   masks = segment(image=raw["image"])

**Node shorthand** --- passing a node directly resolves to the upstream
column whose name equals the **keyword argument**. ``segment(image=raw)``
is exactly equivalent to ``segment(image=raw["image"])``; if ``raw`` has
no ``image`` column the call raises
:class:`~bioimageflow.ColumnNotFoundError`. This is a syntactic shortcut,
not type-based matching --- specs.md §4.6 is explicit that there is no
implicit name-based or type-based column matching at the binding rule
itself.

.. code-block:: python

   # Equivalent to segment(image=raw["image"]):
   masks = segment(image=raw)

**Constant** --- literal Python values:

.. code-block:: python

   masks = segment(image=raw["image"], threshold=0.5)

Missing required inputs (no default, no binding) raise
:class:`~bioimageflow.BindingError`.

Source nodes
------------

Source nodes have no upstream — they produce the initial DataFrame from
their parameters alone. There are two flavours:

**DataFrameTool with** ``accepts_upstream = False`` **.** The canonical
shape for "load files" or "build a parameter sweep". Specs.md §4.2 lists
this as the recommended source pattern. The companion package
``bioimageflow_common_tools`` ships two examples:

.. code-block:: python

   from bioimageflow_common_tools import Files, Generate

   files = Files()
   sigmas = Generate()

   with Workflow() as wf:
       images = files(path="/data", pattern="*.tif")
       sweep = sigmas(column_name="sigma", values=[0.5, 1.0, 2.0])

Constructing a source tool with positional upstream arguments raises
:class:`~bioimageflow.SourceToolUpstreamError`.

**ProcessingTool with no column references.** Useful when the loader
itself needs an isolated environment — for example a DICOM or HDF5 reader
that depends on a heavy native library. The tool reads from constants,
runs in its own environment, and emits a one-row output DataFrame:

.. code-block:: python

   class LoadDicom(ProcessingTool):
       display_name = "Load DICOM"
       environment = EnvironmentSpec(name="dicom", dependencies={"pydicom": "*"})

       class Inputs:
           folder: str

       class Outputs:
           image: Annotated[Path, ImageSpec()] = Template("out.tif")

       def process_row(self, arguments: Arguments) -> "LoadDicom.Outputs":
           ...

Both flavours are valid sources; pick the one that matches the
environment requirements of the loader.

Branching falls out for free
----------------------------

There is no separate "branching" mechanism in BioImageFlow — branches are
just two nodes that read the same upstream column:

.. code-block:: python

   raw = files(path="/data", pattern="*.tif")
   blurred = blur(image=raw["path"])
   thresholded = threshold(image=raw["path"])  # second branch

Reusing two refs in one downstream tool creates a fan-in:

.. code-block:: python

   overlay = compose(image=raw["path"], mask=thresholded["mask"])

Combining branches with explicit join semantics is covered in
:doc:`../tutorials/merge_strategies`.

Named nodes
-----------

By default, nodes take the tool's ``name`` attribute. Use the ``name`` keyword
to disambiguate multiple uses of the same tool:

.. code-block:: python

   blur = GaussianBlur()

   smooth_1 = blur(image=raw["image"], sigma=1.0, name="blur_fine")
   smooth_5 = blur(image=raw["image"], sigma=5.0, name="blur_coarse")

Node names must be unique within a workflow.

DAG validation
--------------

Construction-time errors (cycles, missing bindings, type mismatches,
duplicate names, ...) are reported through the validation surface; the
full ``ValidationErrorKind`` table lives in :doc:`../reference/index`
under the errors page once it lands. For now the exhaustive contract is
in :doc:`../specs` §6.6.
