Merge Strategies
================

When a tool needs data from multiple upstream nodes, the framework must combine
their DataFrames. ``bioimageflow-common-tools`` provides several merge
strategies as :class:`~bioimageflow.DataFrameTool` subclasses.

InnerJoin (default)
-------------------

:class:`~bioimageflow_common_tools.InnerJoin` joins DataFrames on their index. This is the
default behaviour when a ProcessingTool receives inputs from multiple upstream
nodes.

.. code-block:: python

   from bioimageflow import Workflow
   from bioimageflow_common_tools import InnerJoin

   join = InnerJoin()

   with Workflow(storage_path="./results") as wf:
       images = loader(folder="/data")
       masks = segment(image=images["image"])
       # Explicitly join two DataFrames
       merged = join(images, masks)
       result = wf.compute(merged)

CrossJoin
---------

:class:`~bioimageflow_common_tools.CrossJoin` produces the Cartesian product of two
DataFrames. Useful for applying every parameter combination to every image:

.. code-block:: python

   from bioimageflow_common_tools import CrossJoin

   cross = CrossJoin()

   with Workflow(storage_path="./results") as wf:
       images = loader(folder="/data")         # 10 images
       params = param_source()                  # 3 parameter sets
       combos = cross(images, params)           # 30 rows
       results = process(image=combos["image"], sigma=combos["sigma"])
       result = wf.compute(results)

Use ``suffixes`` to resolve column name conflicts:

.. code-block:: python

   cross = CrossJoin()
   combos = cross(left_node, right_node, suffixes=("_img", "_param"))

JoinOnColumn
------------

:class:`~bioimageflow_common_tools.JoinOnColumn` joins on a named column rather than the
index:

.. code-block:: python

   from bioimageflow_common_tools import JoinOnColumn

   join = JoinOnColumn()

   with Workflow(storage_path="./results") as wf:
       images = loader(folder="/data")
       metadata = csv_source(path="metadata.csv")
       merged = join(images, metadata, join_column="sample_id", how="left")
       result = wf.compute(merged)

Parameters:

- ``join_column``: the column to join on (required)
- ``how``: join type --- ``"inner"``, ``"left"``, ``"right"``, ``"outer"``
  (default: ``"inner"``)
- ``suffixes``: tuple of suffixes for overlapping column names

Concat
------

:class:`~bioimageflow_common_tools.Concat` stacks DataFrames vertically. Use it to combine
results from parallel branches:

.. code-block:: python

   from bioimageflow_common_tools import Concat

   concat = Concat()

   with Workflow(storage_path="./results") as wf:
       batch_a = loader(folder="/data/batch_a")
       batch_b = loader(folder="/data/batch_b", name="loader_b")
       all_images = concat(batch_a, batch_b)
       result = wf.compute(all_images)

Collect
-------

:class:`~bioimageflow_common_tools.Collect` gathers specific columns from multiple ancestor
nodes into a single DataFrame. Its output uses
:class:`~bioimageflow.Passthrough`, preserving input columns:

.. code-block:: python

   from bioimageflow_common_tools import Collect

   collect = Collect()

   with Workflow(storage_path="./results") as wf:
       raw = loader(folder="/data")
       masks = segment(image=raw["image"])
       stats = measure(mask=masks["mask"])
       # Collect columns from different nodes
       gathered = collect(raw, masks, stats)
       result = wf.compute(gathered)

Choosing the right strategy
---------------------------

============  =================================================================
Strategy      When to use
============  =================================================================
InnerJoin     Rows correspond 1:1 (same source, same indices)
CrossJoin     Every combination of rows (parameter sweeps)
JoinOnColumn  Match rows by a shared column value (e.g., sample ID)
Concat        Stack independent batches vertically
Collect       Gather columns from multiple points in the DAG
============  =================================================================

Construction-time column validation
-----------------------------------

When both upstream nodes have a known schema, the merged schema is also known
at construction time, and ``node["col"]`` validates immediately. For example,
:class:`~bioimageflow_common_tools.CrossJoin` of two :class:`~bioimageflow_common_tools.Generate`
nodes resolves to the union of their declared column names:

.. code-block:: python

   from bioimageflow_common_tools import CrossJoin, Files, Generate

   with Workflow(storage_path="./results"):
       files = Files()(path="./data")
       sens = Generate()(column_name="sensitivity", values=[0.1, 0.2])
       size = Generate()(column_name="size", values=[1, 2])
       grid = CrossJoin()(files, sens, size)
       # The merged schema {"path", "sensitivity", "size"} is
       # resolved at construction time, so this raises immediately if you
       # mistype the column name:
       ref = grid["sensitivity"]   # OK
       # ref = grid["sensitvity"]  # ColumnNotFoundError
