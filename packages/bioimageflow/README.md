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

For workspace development, use the repository root:

```bash
uv sync
uv run pytest packages/bioimageflow tests
```

Wetlands worker environments install `bioimageflow-core` independently from the orchestrator environment.
By default `WetlandsEnvManager` injects `bioimageflow-core==<installed version>` for reproducible runtime environments.
Set `BIOIMAGEFLOW_USE_LOCAL_CORE=1` while developing from a source checkout to inject the local editable `bioimageflow-core` project into newly created worker environments.
Existing Wetlands workspaces are not migrated automatically, so recreate stale tool workspaces after changing this setting.
