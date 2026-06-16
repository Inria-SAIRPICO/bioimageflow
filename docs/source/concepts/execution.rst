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

3. **Cache lookup**: for each node, derive its v1 result key from the node,
   resolved inputs, tool identity, environment identity, and selected upstream
   records. If ``current.json`` selects a reusable record, load it and skip
   execution.

4. **Input resolution**: for each row, resolve column bindings and constants
   into concrete values. Output path templates are resolved at this stage.

5. **Tool execution**:

   - **DataFrameTool**: call ``merge_dataframes`` (if multiple upstreams),
     then ``transform``. Runs in the main process.
   - **ProcessingTool**: call ``process_row`` for each row (or
     ``process_batch`` for all rows). Runs in the tool's declared environment.

6. **Result assembly**: collect outputs into a DataFrame. For ProcessingTools,
   each ``Outputs`` instance becomes a row.

7. **Cache publication**: publish the output DataFrame and owned assets as an
   immutable v1 record, then select it through ``current.json``.

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
   ├── cache/
   │   └── v1/
   │       └── results/
   │           └── {shard}/{shard}/{result_key}/
   │               ├── result.json
   │               ├── current.json
   │               ├── attempts/{attempt_id}/staging/
   │               └── records/{record_id}/
   │                   ├── manifest.json
   │                   ├── dataframe.parquet
   │                   └── assets/
   ├── runs/
   └── latest/

``cache/v1`` is the canonical machine-readable cache root. ``runs/`` and
``latest/`` are human-facing provenance views over selected records and are
not used to decide cache hits.

Result keys
-----------

The v1 result key is the public cache identity. It changes when any logical
input to the node's result changes, including selected upstream records. The
engine may keep a diagnostic logical signature internally during migration,
but public planning, progress, invalidation, and run-view APIs use
``result_key`` and ``record_id``.

Progress events
---------------

The engine emits :class:`~bioimageflow.ProgressEvent` objects via the
``on_progress`` callback:

- ``started``: node execution begins
- ``row_complete``: a single row finished (with ``row`` and ``total_rows``)
- ``completed``: node execution finished
- ``cached``: node result loaded from cache (no execution)

Cache-related events expose v1 ``result_key`` / ``record_id`` values, never
legacy cache directory names or diagnostic signature hashes.

See also
--------

- :doc:`caching` --- the v1 result-key/current-record model, ``plan()``,
  ``invalidate()``, and ``NodePlanStatus`` values.
