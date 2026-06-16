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

Automatic cache retention through ``max_executions`` or ``max_age`` is not part of the clean ``Workflow`` API.
Future pruning must be an explicit storage maintenance operation.

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

Default engine
--------------

The default engine runs **independent nodes concurrently** and dispatches
**rows in parallel** across Wetlands worker processes — one process pool per
declared :class:`~bioimageflow_core.EnvironmentSpec`. See
:doc:`../tutorials/parallelism` for ``max_workers``,
:class:`~bioimageflow_core.ResourceSpec`, and per-environment overrides. A
deterministic ``execution="sequential"`` mode is available for debugging.

Storage layout
--------------

.. code-block:: text

   {storage_path}/
   └── data/
       └── {node_name}/
           └── {YYYYMMDD_HHMMSS}_{hash[:12]}/
               ├── dataframe.csv     # output DataFrame
               ├── metadata.json     # tool class name, version, timestamp
               ├── parameters.json   # resolved parameter values
               └── assets/           # output files

Each cache entry lives in a directory named with a wall-clock timestamp and
the first twelve characters of the signature hash (specs.md §7.2). The
``assets/`` directory contains files produced by ProcessingTools (images,
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

See also
--------

- :doc:`caching` --- the signature-hash model, ``plan()``, ``invalidate()``,
  and the four ``NodePlanStatus`` values.
