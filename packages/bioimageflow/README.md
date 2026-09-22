# bioimageflow

Main-process orchestrator for BioImageFlow workflows.

This package builds workflow DAGs, resolves column bindings, executes tools, and publishes cache records and run views.
Worker-safe tool authoring primitives live in `bioimageflow-core`; optional domain tools live in separate `bioimageflow-*-tools` packages.

Install:

```bash
pip install bioimageflow
```

Install the optional distributed runtime when using `ParslEngine`:

```bash
pip install "bioimageflow[parsl]"
```

Install the PSI/J cluster orchestrator launcher with Parsl:

```bash
pip install "bioimageflow[parsl,psij]"
```

For workspace development, use the repository root:

```bash
uv sync
uv run pytest packages/bioimageflow tests
```

Wetlands worker environments install `bioimageflow-core` independently from the orchestrator environment.
By default `WetlandsEnvManager` injects `bioimageflow-core==<installed version>` for reproducible runtime environments.
For regular source development, set `BIOIMAGEFLOW_CORE_SOURCE=/absolute/path/to/bioimageflow-core` to validate and inject that project as an editable dependency without installing it into the orchestrator environment.
The legacy `BIOIMAGEFLOW_USE_LOCAL_CORE=1` mode remains available when the orchestrator already imports `bioimageflow-core` from an editable source checkout.
