# bioimageflow-core

Worker-safe core APIs for BioImageFlow tools.

This package contains `ProcessingTool`, `IOModel`, `Arguments`, `ExecutionContext`, `Template`, `EnvironmentSpec`, image type metadata, portable `ViewerSpec`/`NapariRequirement` metadata, and shared-memory helpers.
It is installed in the main process and in tool worker environments.
It declares NumPy because shared-memory helpers expose NumPy array views at runtime.
It declares `packaging` to validate portable PEP 440 viewer-package constraints without importing napari or plugin discovery code.
The `bioimageflow` orchestrator injects a pinned published `bioimageflow-core` package into Wetlands worker environments by default.
During source development, set `BIOIMAGEFLOW_USE_LOCAL_CORE=1` before creating worker environments to inject this local editable project instead.

Install:

```bash
pip install bioimageflow-core
```

For workspace development, use the repository root:

```bash
uv sync
uv run pytest packages/bioimageflow-core tests/unit/test_core_package_metadata.py
```
