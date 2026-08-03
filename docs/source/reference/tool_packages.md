# Tool Package Release Contract

BioImageFlow tool packages are optional distributions that group tools by workflow domain.
Package-owned user documentation is published in the top-level [Tool Packages](../tool_packages/index.md) section, where each package has its own page tree for tools and workflows.

This reference page keeps the release, packaging, and CI contract shared by first-party tool packages.

## Release and CI Contract

The orchestrator, core package, and each companion tool package own independent versions.
The repository is tested as one workspace, while package-specific annotated tags select one distribution for publication.
See [Releasing Python Packages](releasing.md) for the release tag contract, status tool, CI workflow, and operator procedure.
The orchestrator and first-party tool packages declare Python `>=3.10`; `bioimageflow-core` declares Python `>=3.9` so Wetlands worker environments with Python 3.9-only binary dependencies can install the shared worker API.
The deterministic CI matrix validates the main development/runtime surface with full fast coverage on Python 3.10 and 3.12, plus Python 3.11 compatibility smoke on every pipeline.
Full deterministic Python 3.11 validation is manually available before tagging and rerun as a required release gate, while static compatibility tests keep `bioimageflow-core` import syntax compatible with Python 3.9.

Package metadata separates distribution dependencies from isolated runtime dependencies.
Install-time dependencies must stay small enough for package import, documentation discovery, and metadata validation.
Heavy or tool-specific runtimes belong in the tool's `EnvironmentSpec`, not in the package import path.
Package-local `uv.sources` entries mirror first-party runtime dependencies so editable workspace runs and built artifacts use the same package graph.
Published first-party dependency requirements declare the oldest tested compatible version and an upper compatibility boundary.
Downstream packages are released only when their code, packaged content, or compatibility requirements change.

The regular CI gate for package and documentation changes includes:

```bash
uv run ruff check .
uv run pyright
uv run pytest tests -m "not slow and not acceptance and not packaging and not package_tools and not complete and not wetlands and not public_data and not external_binary and not sairpico_binary and not model_runtime"
uv run pytest -m "acceptance and not complete"
uv run pytest -m "package_tools and not complete"
uv run pytest tests/unit/test_package_artifacts.py
uv build --all-packages --no-sources --out-dir dist/packages
BIOIMAGEFLOW_PACKAGE_ARTIFACTS_DIR=dist/packages uv run pytest tests/unit/test_package_artifacts.py
uv run sphinx-build -W --keep-going docs/source docs/_build/html
```

The resource-dependent complete-test jobs run weekly to detect external drift and can also be selected manually before a relevant release.
They are useful release evidence, but deterministic unit, package-artifact, and documentation checks remain the required proof for ordinary package changes.

Wheels exclude package documentation, package tests, generated build outputs, and local caches.
Source distributions keep package docs and tests so release artifacts remain auditable without bloating installed wheels.
Release metadata must not expose broad extras that silently install all domain runtimes; users install the companion packages and isolated tool environments they actually need.
Publishing is an approval-gated GitHub Actions deployment triggered by a protected package-specific release tag.
The release job reruns deterministic Python 3.11 validation, builds only the tagged distribution with workspace sources disabled, validates the exact artifacts, and uploads them to PyPI.

## Package-Owned Documentation Contract

Each package owns its README, `docs/index.md`, tool pages, workflow pages, tests, fixtures, examples, and small runtime assets.
Every public tool and workflow should have package-local documentation and deterministic tests.
The main docs include first-party package docs through generated wrapper pages, but the source of truth remains in each package directory.

Custom packages can opt into local documentation builds with `[tool.bioimageflow.docs]` metadata in their `pyproject.toml`.
See the [custom tool package how-to guide](../how-to/custom_tool_package) for the package layout, documentation contract, and test expectations.
