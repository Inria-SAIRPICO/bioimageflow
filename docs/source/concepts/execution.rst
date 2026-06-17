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

Engines and scheduling
----------------------

The default engine is ``engine="direct"`` with ``execution="parallel"``.
It runs :class:`~bioimageflow.DataFrameTool` objects and
:class:`~bioimageflow_core.ProcessingTool` methods in the orchestrator process.
The parallel scheduling policy may execute ready independent nodes concurrently,
but row-level worker-process parallelism is only used by
``engine="wetlands"``.

Use ``engine="wetlands"`` when processing tools should run in isolated Wetlands
worker environments. In that backend, ``max_workers`` and
per-environment overrides control worker-process pools and row dispatch.
See :doc:`../tutorials/parallelism` for ``max_workers``,
:class:`~bioimageflow_core.ResourceSpec`, and per-environment overrides.
Use ``execution="sequential"`` for single-node-at-a-time debugging.

Storage layout
--------------

.. code-block:: text

   {storage_path}/
   ├── cache/
   │   └── v1/
   │       └── results/
   │           └── {shard}/{shard}/{result_key}/
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

The v1 result key is the public cache identity exposed by planning, progress,
invalidation, and run-view APIs. Current runtime result keys are transitional:
they wrap the node's diagnostic logical signature in a v1 ``rk_...`` key while
the storage layout already uses immutable records and ``current.json``.
The target v1 material is documented in :doc:`../reference/output_cache_storage`.

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
