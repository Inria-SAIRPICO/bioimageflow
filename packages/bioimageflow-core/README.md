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

Shared-memory creation and attachment helpers reject object-containing NumPy dtypes before allocation or attachment.
Numeric data and Path/str image dispatch keep their existing behavior; local handles close on context exit while segment unlinking remains with the caller/engine owner.

Current processing transport uses `ProcessingTask`, `RowInvocation`, `ProcessingTaskResult`, and `RowResult` with explicit task/result v2 envelopes.
The public task/result codecs use the same typed grammar for arguments and outputs: primitive leaves, explicit dictionary/list/tuple nodes, Paths, SharedArray references, NumPy arrays, and dtype-preserving numeric NumPy scalars.
Literal dictionaries cannot collide with typed descriptors, and SharedArray decoding never allocates or attaches memory.
Unsupported objects, cyclic containers, object-containing dtypes, and malformed descriptors are refused; picklability alone is not sufficient.
SharedArray references are host-local, so distributed callers such as Parsl must refuse them even though local Wetlands workers can transport the reference.
