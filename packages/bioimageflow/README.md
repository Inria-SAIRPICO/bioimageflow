# bioimageflow

Main-process orchestrator for BioImageFlow workflows.

This package builds workflow DAGs, resolves column bindings, executes tools, and publishes v1 cache records and run views.
Worker-safe tool authoring primitives live in `bioimageflow-core`; optional domain tools live in separate `bioimageflow-*-tools` packages.

Install:

```bash
pip install bioimageflow
```

For workspace development, use the repository root:

```bash
uv sync
uv run pytest packages/bioimageflow tests
```
