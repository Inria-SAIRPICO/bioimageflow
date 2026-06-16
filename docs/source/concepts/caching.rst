Caching and Provenance
======================

BioImageFlow caches the output of every node. When you re-run a workflow,
only nodes whose inputs or parameters have changed are recomputed.

How caching works
-----------------

Each node has a v1 **result key** under ``storage_path/cache/v1/``.
The result key identifies the reusable cache selection for the node's logical inputs, parameters, environment, and upstream cache state.
Each successful execution publishes an immutable **record**, and ``current.json`` selects the record that cache hits must use.

The implementation also computes a diagnostic logical signature from:

- Tool class name and version
- Environment hash (dependencies)
- Resolved parameter values
- Upstream node hashes (recursive)
- Source-code hash (``dev_mode`` only)

This signature is exposed as ``NodePlan.logical_signature`` for diagnostics, but it is not the public v1 cache identity.
Use ``NodePlan.final_result_key`` and ``NodePlan.selected_record_id`` for cache/provenance state.
If ``current.json`` selects a valid record for a node's final result key, the cached DataFrame is loaded instead of re-executing the tool.

.. note::

   ``dev_mode=True`` adds the tool's source code hash to the signature, so
   editing ``process_row`` invalidates the cache. Leave it off in production —
   only the package version should matter then. Pass it as
   ``wf.compute(target, dev_mode=True)`` or ``wf.plan(dev_mode=True)``.

Cache location
--------------

Results are stored under the ``storage_path`` you pass to
:class:`~bioimageflow.Workflow`:

.. code-block:: text

   bif_data/
   └── cache/
       └── v1/
           └── results/
               └── <result-shard>/
                   └── <result-key>/
                       ├── current.json
                       └── records/
                           └── <record-id>/
                               ├── dataframe.parquet
                               ├── manifest.json
                               └── assets/

Immutable records are not deleted by normal invalidation.
Invalidation removes cache selection state such as ``current.json``.

What invalidates the cache
--------------------------

Any of these changes produce a different v1 result key:

- **Parameter change**: e.g., ``sigma=1.0`` to ``sigma=2.0``
- **Upstream change**: if a parent node's hash changes, all descendants
  recompute
- **Tool version change**: updating the package version of a tool
- **Source code change** (``dev_mode`` only): modifying the tool's Python
  source

Inspecting cache state without executing
----------------------------------------

:meth:`~bioimageflow.Workflow.plan` returns a per-node plan **without
running any tool code** — useful for GUIs that want to render cache state, or
for scripts that need to see what ``compute()`` would do:

.. code-block:: python

   from bioimageflow import NodePlanStatus

   plan = wf.plan()                          # dict[str, NodePlan]
   for name, entry in plan.items():
       print(name, entry.status, entry.final_result_key, entry.selected_record_id)

Each :class:`~bioimageflow.engine.NodePlan` carries:

- ``node_name`` — scoped name (``"outer/inner_1"`` for sub-workflow internals)
- ``final_result_key`` — v1 result key when all required upstream selected records are known
- ``selected_record_id`` — selected immutable record ID when the node is cached
- ``status`` — one of the five
  :class:`~bioimageflow.engine.NodePlanStatus` values below
- ``upstream`` — scoped names of this node's direct upstreams
- ``pending_upstreams`` — upstream nodes whose selected records must be produced before this node's final key is known
- ``logical_signature`` — diagnostic logical signature, not a cache key

.. list-table::
   :header-rows: 1
   :widths: 18 50 32

   * - Status
     - Meaning
     - Suggested UI affordance
   * - ``CACHED``
     - ``current.json`` selects a valid reusable record for the final result key; ``compute()`` would short-circuit if it consumes the same upstream records.
     - Green / "up to date"
   * - ``OUT_OF_DATE``
     - Prior records exist for this node or lineage, but no selected record matches the current final result key.
     - Yellow / "needs rebuild"
   * - ``UNEXECUTED``
     - No reusable record exists yet for this node/result lineage.
     - Grey / "not yet run"
   * - ``PENDING_UPSTREAM``
     - At least one upstream selected record is not known yet, so the final result key cannot be reported.
     - Grey / "waiting on upstream"
   * - ``SKIPPED``
     - The node is disabled, or has a disabled upstream that prevents
       execution.
     - Struck-through / muted

A sub-workflow node aggregates its internals: it reports ``CACHED`` only when every internal entry is cached.

``plan()`` does **not** start Wetlands worker pools.
It uses the direct planning path and does not run tool code.

Invalidating cache when a parameter changes
-------------------------------------------

When a workflow lives in a long-running process (a GUI, a server) and an
upstream parameter changes, you may want to clear cached results explicitly
rather than rely on hash drift to trigger re-execution.
:meth:`~bioimageflow.Workflow.invalidate` clears the cache selection for the
named nodes — and, by default, the selection of every node transitively
downstream:

.. code-block:: python

   cleared = wf.invalidate(["segment"])      # cascade=True is the default
   cleared_nodes = {selection.node_name for selection in cleared}

   wf.invalidate(["segment"], cascade=False) # just "segment"

The return value is a set of ``InvalidatedSelection`` entries for v1 ``current.json`` pointers that were actually removed.
Each entry reports the node name, result key, selected record ID when it was readable, and whether the pointer was removed normally or as corrupt metadata.
Passing an unknown name raises ``KeyError``.

.. warning::

   ``invalidate()`` is **not safe** to call concurrently with ``compute()``
   on the same workflow. The library does not currently expose a public
   lock primitive; callers that need to invalidate while a compute is in
   flight must coordinate externally (cancel + join + invalidate).

Forcing re-execution
--------------------

The recommended recipe is :meth:`~bioimageflow.Workflow.invalidate`:

.. code-block:: python

   wf.invalidate(["segment"])  # invalidates segment + everything downstream
   result = wf.compute(target)

Changing a parameter value works as a fallback — even a trivial change
produces a new cache key.

Cache cleanup
-------------

``max_executions`` and ``max_age`` are removed from the clean ``Workflow`` API.
Automatic deletion of published cache records is not part of v1 runtime execution.
Future pruning must be exposed as an explicit storage maintenance operation.
