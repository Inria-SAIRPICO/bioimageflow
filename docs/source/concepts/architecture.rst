Architecture
============

BioImageFlow is split into two Python packages with a clear separation of
concerns.

.. code-block:: text

   bioimageflow-core           bioimageflow
   (zero deps, worker-safe)    (pandas + pydantic, main process)
   ┌──────────────────────┐   ┌───────────────────────────┐
   │  Semantic, Layout     │   │  Workflow                  │
   │  ImageSpec, image I/O  │   │  Node, ColumnRef           │
   │  ProcessingTool       │   │  SequentialEngine          │
   │  IOModel, Arguments   │   │  DataFrameTool, Passthrough│
   │  EnvironmentSpec      │   │  Merge strategies          │
   │  SharedArray, I/O     │   │  Cache, Storage, Template  │
   └──────────────────────┘   └───────────────────────────┘

bioimageflow-core
-----------------

**Zero external dependencies.** Uses only the Python standard library.

This package is installed in *every* environment --- the main process and all
worker environments. It contains:

- **Type system**: :class:`~bioimageflow_core.Semantic`,
  :class:`~bioimageflow_core.Layout`, :class:`~bioimageflow_core.ImageSpec`,
  :data:`~bioimageflow_core.SCALAR_IMAGE_SEMANTICS`,
  :func:`~bioimageflow_core.ImageShared`
- **Tool base classes**: :class:`~bioimageflow_core.BaseTool`,
  :class:`~bioimageflow_core.ProcessingTool`, :class:`~bioimageflow_core.IOModel`
- **Argument passing**: :class:`~bioimageflow_core.Arguments`
- **Environment specs**: :class:`~bioimageflow_core.EnvironmentSpec`,
  :class:`~bioimageflow_core.ResourceSpec`
- **Shared memory**: :class:`~bioimageflow_core.SharedArray`,
  :func:`~bioimageflow_core.shm.create_shared_output`,
  :func:`~bioimageflow_core.shm.open_shared_array`
- **I/O dispatch**: :func:`~bioimageflow_core.io.load_image`,
  :func:`~bioimageflow_core.io.save_image`

The zero-dependency constraint means workers only need ``bioimageflow-core``
plus their own domain libraries (e.g., cellpose, scikit-image). They never
import pandas or pydantic.

bioimageflow
------------

**Depends on pandas and pydantic.** Main-process only.

This package is the orchestrator. It:

1. **Builds the DAG** from tool calls and column bindings
2. **Resolves inputs** by matching column references and constants
3. **Executes nodes** in topological order
4. **Manages caching** via signature hashes
5. **Stores results** as DataFrames and asset files

Key classes:

- :class:`~bioimageflow.Workflow` --- entry point, context manager
- :class:`~bioimageflow.Node` / :class:`~bioimageflow.ColumnRef` --- graph
  primitives
- :class:`~bioimageflow.DataFrameTool` --- main-process DataFrame transforms
- Merge strategies: :class:`~bioimageflow.InnerJoin`,
  :class:`~bioimageflow.CrossJoin`, :class:`~bioimageflow.JoinOnColumn`,
  :class:`~bioimageflow.Concat`, :class:`~bioimageflow.Collect`

Plus ``WorkflowSession``, ``ToolRegistry``, and the validation surface for
GUI / platform integrators — see the :doc:`GUI / Platform Integrators tree
</gui/index>` for those.

bioimageflow-common-tools
-------------------------

A third, **layered** package — ``bioimageflow_common_tools`` — ships the
canonical source tools (``Files``, ``Generate``) and the basic processing
tools used throughout the documentation (``ConvertImage``, ``ExtractChannel``,
``CellposeSAM``, the merge tools, ...). It depends on ``bioimageflow`` and
``bioimageflow-core``; it is not imported by either of them.

The docs use it freely so examples are short and runnable. Workflow authors
can rely on it directly, write their own tools following the same patterns,
or mix both. See :doc:`/installation` for the install command.

Why two packages?
-----------------

BioImageFlow targets bioimage analysis where tools often have heavy, conflicting
dependencies (e.g., different PyTorch versions, GPU libraries). The two-package
split ensures:

1. **Workers stay lightweight.** A Cellpose worker installs
   ``bioimageflow-core`` + ``cellpose`` --- no pandas, no pydantic, no
   orchestrator overhead.

2. **No import conflicts.** The orchestrator's dependencies (pandas, pydantic)
   never leak into worker environments.

3. **Clear boundary.** Tool authors only depend on ``bioimageflow-core``. They
   never import from ``bioimageflow``.

Data flow
---------

.. code-block:: text

   ┌─────────┐   DataFrame   ┌─────────┐   DataFrame   ┌─────────┐
   │  Node A  │──────────────>│  Node B  │──────────────>│  Node C  │
   └─────────┘               └─────────┘               └─────────┘
       │                          │                          │
       ▼                          ▼                          ▼
   bif_data/data/a/           bif_data/data/b/           bif_data/data/c/
   └── <hash>/                └── <hash>/                └── <hash>/
       ├── dataframe.csv          ├── dataframe.csv          ├── dataframe.csv
       └── assets/                └── assets/                └── assets/

Each node receives a DataFrame from its upstream nodes, executes its tool, and
produces a new DataFrame. The DataFrame and any file assets are persisted under
the node's cache directory, keyed by signature hash.
