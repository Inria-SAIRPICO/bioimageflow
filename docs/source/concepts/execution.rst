Execution
=========

This page explains what happens when you call
:meth:`~bioimageflow.Workflow.compute`.

Execution pipeline
------------------

1. **Target resolution**: identify which nodes are needed to produce the
   requested targets.

2. **Topological sort**: order the reachable nodes so that every node executes
   after its dependencies. Uses Kahn's algorithm; cycles are detected and
   rejected.

3. **Cache lookup**: for each node, compute its signature hash and check if a
   cached result exists. If so, load the cached DataFrame and skip execution.

4. **Input resolution**: for each row, resolve column bindings and constants
   into concrete values. Output path templates are resolved at this stage.

5. **Tool execution**:

   - **DataFrameTool**: call ``merge_dataframes`` (if multiple upstreams),
     then ``transform``. Runs in the main process.
   - **ProcessingTool**: call ``process_row`` for each row (or
     ``process_batch`` for all rows). Runs in the tool's declared environment.

6. **Result assembly**: collect outputs into a DataFrame. For ProcessingTools,
   each ``Outputs`` instance becomes a row.

7. **Cache save**: persist the output DataFrame and assets.

8. **Cache cleanup**: if ``max_executions`` or ``max_age`` is set, remove old
   cache entries.

Index alignment
---------------

When a ProcessingTool receives inputs from multiple upstream nodes, the engine
aligns rows by index. Row 0 of node A matches row 0 of node B.

For explosion tools (one-to-many), child indices use ``::`` separators:

.. code-block:: text

   Parent index: "0"
   Child indices: "0::0", "0::1", "0::2"

Downstream tools receiving both parent and child data use the parent prefix to
align rows correctly.

Sequential engine
-----------------

The current engine is :class:`~bioimageflow.engine.SequentialEngine`. It
executes nodes one at a time, rows one at a time. Future engines may add
parallel or distributed execution.

Storage layout
--------------

.. code-block:: text

   {storage_path}/
   └── data/
       └── {node_name}/
           └── {signature_hash}/
               ├── dataframe.csv     # output DataFrame
               ├── metadata.json     # tool name, version, timestamp
               ├── parameters.json   # resolved parameter values
               └── assets/           # output files

The ``assets/`` directory contains files produced by ProcessingTools (images,
reports, etc.). Output path templates resolve to paths within this directory.

Signature hash
--------------

The signature hash uniquely identifies an execution configuration:

.. code-block:: text

   SHA256(
       tool_name
     + tool_version
     + environment_hash
     + sorted(resolved_parameters)
     + sorted(upstream_signature_hashes)
     + source_hash (dev_mode only)
   )

Any change to inputs, parameters, tool version, or upstream results produces a
different hash, triggering re-execution.

Progress events
---------------

The engine emits :class:`~bioimageflow.ProgressEvent` objects via the
``on_progress`` callback:

- ``started``: node execution begins
- ``row_complete``: a single row finished (with ``row`` and ``total_rows``)
- ``completed``: node execution finished
- ``cached``: node result loaded from cache (no execution)
