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

**Node shorthand** --- passing a node directly auto-resolves to a matching
column:

.. code-block:: python

   # If raw has a single output column matching the input type,
   # the framework resolves it automatically
   masks = segment(image=raw)

**Constant** --- literal Python values:

.. code-block:: python

   masks = segment(image=raw["image"], threshold=0.5)

Missing required inputs (no default, no binding) raise
:class:`~bioimageflow.BindingError`.

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

The engine rejects:

- **Cycles**: tool A depends on tool B which depends on tool A
- **Missing bindings**: required inputs without a source
- **Type mismatches**: incompatible image specs between producer and consumer
- **Duplicate names**: two nodes with the same name
