# Tool Package Strategy

Domain-specific tools are organized in dedicated **Tool packages**.
Each package owns its Python code, package-local tests, source documentation, tool pages, workflow pages, and optional complete tests for public data or external runtimes.

Package documentation lives under `packages/<package-name>/docs/`.
The main Sphinx documentation includes those package-owned pages through generated wrappers under `docs/source/tool_packages/`.
Regenerate wrappers with:

```bash
uv run python docs/generate_tool_package_docs.py
```

Run the check mode in CI or before release:

```bash
uv run python docs/generate_tool_package_docs.py --check
```


