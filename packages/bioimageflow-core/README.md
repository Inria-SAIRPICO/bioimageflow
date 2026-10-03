# bioimageflow-core

Worker-safe core APIs for BioImageFlow tools.

This package contains `ProcessingTool`, `IOModel`, `Arguments`, `ExecutionContext`, `Template`, `EnvironmentSpec`, image type metadata, portable `ViewerSpec`/`NapariRequirement` metadata, and shared-memory helpers.
It is installed in the main process and in tool worker environments.
It declares NumPy because shared-memory helpers expose NumPy array views at runtime.
It declares `packaging` to validate portable PEP 440 viewer-package constraints without importing napari or plugin discovery code.
The `bioimageflow` orchestrator injects a pinned published `bioimageflow-core` package into Wetlands worker environments by default.
During source development, set `BIOIMAGEFLOW_CORE_SOURCE` to this project directory before creating worker environments to validate and inject it as an editable dependency.
The legacy `BIOIMAGEFLOW_USE_LOCAL_CORE=1` mode requires the orchestrator environment to import this package from its editable source checkout.

Install:

```bash
pip install bioimageflow-core
```

For workspace development, use the repository root:

```bash
uv sync
uv run pytest packages/bioimageflow-core tests/unit/test_core_package_metadata.py
```

Numeric creation/mapping helpers reject object-containing NumPy dtypes before backing allocation or mapping.
Numeric data and Path/str image dispatch retain their essential behavior; explicit owners and live reader/worker grants control file lifetime.

Current processing transport uses `ProcessingTask`, `RowInvocation`, `ProcessingTaskResult`, and `RowResult` with explicit task/result v2 envelopes.
The public task/result codecs use the same typed grammar for arguments and outputs: primitive leaves, explicit dictionary/list/tuple nodes, Paths, SharedArray references, NumPy arrays, and dtype-preserving numeric NumPy scalars.
Literal dictionaries cannot collide with typed descriptors, and SharedArray decoding never allocates or attaches memory.
Unsupported objects, cyclic containers, object-containing dtypes, and malformed descriptors are refused; picklability alone is not sufficient.
SharedArray references are host-local, so distributed callers such as Parsl must refuse them even though local Wetlands workers can transport the reference.

### Shared-array ownership

SharedArray uses scoped numeric NPY2 file-backed mmap storage, with one initial copy and zero-copy mapped views thereafter.
Allocate under an explicit `SharedMemoryContext.activate()`; helper/context exit only unbinds.
References carry a strong local owner excluded from equality and wire; pure typed decoding is attachment-free, and workers borrow explicit task/input descriptors.
Controller close/release refuses new access while mapped views and admitted workers retain backing; `CleanupStatus` reports pending readers/grants/files/errors until physical drain.
Workflow/engine completion never auto-closes returned references; no resource tracker or private unregister is involved.
