Caching and Provenance
======================

BioImageFlow caches the output of every node. When you re-run a workflow,
only nodes whose inputs or parameters have changed are recomputed.

How caching works
-----------------

Each execution produces a **signature hash** computed from:

- Tool class name and version
- Environment hash (dependencies)
- Resolved parameter values
- Upstream node hashes (recursive)
- Source-code hash (``dev_mode`` only)

If the signature hash matches a previous run, the cached DataFrame is loaded
instead of re-executing the tool.

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
   └── data/
       └── <node_name>/
           └── <YYYYMMDD_HHMMSS>_<hash[:12]>/
               ├── dataframe.csv     # output DataFrame
               ├── metadata.json     # execution metadata
               ├── parameters.json   # resolved parameters
               └── assets/           # output files (images, etc.)

Each cache entry directory carries a wall-clock timestamp and the first
twelve characters of the signature hash (specs.md §7.2).

What invalidates the cache
--------------------------

Any of these changes produce a different signature hash:

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
       print(name, entry.status, entry.sig_hash[:12])

Each :class:`~bioimageflow.engine.NodePlan` carries:

- ``node_name`` — scoped name (``"outer/inner_1"`` for sub-workflow internals)
- ``sig_hash`` — byte-identical to what ``compute()`` would compute; empty
  string for ``SKIPPED`` nodes
- ``status`` — one of the four
  :class:`~bioimageflow.engine.NodePlanStatus` values below
- ``upstream`` — scoped names of this node's direct upstreams

.. list-table::
   :header-rows: 1
   :widths: 18 50 32

   * - Status
     - Meaning
     - Suggested UI affordance
   * - ``CACHED``
     - Current signature hash matches an existing cache entry; ``compute()``
       would short-circuit.
     - Green / "up to date"
   * - ``OUT_OF_DATE``
     - The storage directory has entries from a previous run, but none match
       the current hash. ``compute()`` would re-execute.
     - Yellow / "needs rebuild"
   * - ``UNEXECUTED``
     - No storage directory exists for this node yet — it has never run.
     - Grey / "not yet run"
   * - ``SKIPPED``
     - The node is disabled, or has a disabled upstream that prevents
       execution. ``sig_hash`` is empty.
     - Struck-through / muted

A sub-workflow node aggregates its internals: it reports ``CACHED`` only when
*every* internal entry is cached, ``UNEXECUTED`` otherwise.

``plan()`` does **not** start Wetlands worker pools — it instantiates an
in-process :class:`~bioimageflow.engine.DefaultEngine` with
``use_wetlands=False``. The hashes it returns are byte-identical to what
``compute()`` would produce.

Invalidating cache when a parameter changes
-------------------------------------------

When a workflow lives in a long-running process (a GUI, a server) and an
upstream parameter changes, you may want to clear cached results explicitly
rather than rely on hash drift to trigger re-execution.
:meth:`~bioimageflow.Workflow.invalidate` removes the cache directories of
the named nodes — and, by default, the cache of every node transitively
downstream:

.. code-block:: python

   cleared = wf.invalidate(["segment"])      # cascade=True is the default
   # cleared == {"segment", "measure", "summary"}

   wf.invalidate(["segment"], cascade=False) # just "segment"

The return value is the set of node names whose cache directories were
actually removed (entries that didn't exist on disk are silently skipped).
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

   wf.invalidate(["segment"])  # clears segment + everything downstream
   result = wf.compute(target)

Changing a parameter value works as a fallback — even a trivial change
produces a new hash. Manual ``rm -rf bif_data/data/<node>/`` works too but
bypasses cascade tracking; prefer ``invalidate()``.

Cache cleanup
-------------

Control cache growth with ``max_executions`` and ``max_age``:

.. code-block:: python

   # Keep only the 5 most recent executions per node
   with Workflow(storage_path="./bif_data", max_executions=5) as wf:
       ...

   # Delete cache entries older than 7 days
   from datetime import timedelta
   with Workflow(storage_path="./bif_data", max_age=timedelta(days=7)) as wf:
       ...

Cleanup runs automatically at the end of ``wf.compute()``.
