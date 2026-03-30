Caching and Provenance
======================

BioImageFlow automatically caches the output of every node. When you re-run a
workflow, only nodes whose inputs or parameters have changed are recomputed.

How caching works
-----------------

Each execution produces a **signature hash** computed from:

- Tool class name and version
- Environment hash (dependencies)
- Resolved parameter values
- Upstream node hashes (recursive)

If the signature hash matches a previous run, the cached DataFrame is loaded
instead of re-executing the tool.

Cache location
--------------

Results are stored under the ``storage_path`` you pass to
:class:`~bioimageflow.Workflow`:

.. code-block:: text

   bif_data/
   └── data/
       └── <node_name>/
           └── <signature_hash>/
               ├── dataframe.csv     # output DataFrame
               ├── metadata.json     # execution metadata
               ├── parameters.json   # resolved parameters
               └── assets/           # output files (images, etc.)

What invalidates the cache
--------------------------

Any of these changes produce a different signature hash:

- **Parameter change**: e.g., ``sigma=1.0`` to ``sigma=2.0``
- **Upstream change**: if a parent node's hash changes, all descendants
  recompute
- **Tool version change**: updating the package version of a tool
- **Source code change** (dev mode only): modifying the tool's Python source

Dev mode
--------

Enable ``dev_mode`` to include the tool's source code hash in the signature.
This is useful during development so that editing a tool's ``process_row``
invalidates the cache:

.. code-block:: python

   with Workflow(storage_path="./bif_data") as wf:
       raw = loader(folder="/data")
       masks = segment(image=raw["image"])
       result = wf.compute(masks, dev_mode=True)

In production, leave ``dev_mode=False`` (the default) so that only the package
version matters.

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

Forcing re-execution
--------------------

To force a node to re-execute, delete its cache directory:

.. code-block:: bash

   rm -rf bif_data/data/segment/

Or change a parameter value --- even a trivial change produces a new hash.
