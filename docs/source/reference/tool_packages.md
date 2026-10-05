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
Normal exact-head CI is reused by the coordinated release workflow rather than rerun in a separate release validation job.
Source-disabled Core numeric mapping and process-lifetime witnesses on Linux and Windows, Python 3.9 and current Python, cover the supported array capability beyond syntax checks.

Package metadata separates distribution dependencies from isolated runtime dependencies.
Install-time dependencies must stay small enough for package import, documentation discovery, and metadata validation.
Heavy or tool-specific runtimes belong in the tool's `EnvironmentSpec`, not in the package import path.
Hosts may install a tool distribution with `ToolRegistry.install_package(..., install_dependencies=False)` when they provide compatible main-process dependencies; other callers install declared dependencies by default.
Package-local `uv.sources` entries mirror first-party runtime dependencies so editable workspace runs and built artifacts use the same package graph.
Published first-party dependency requirements declare the tested current support cohort and its upper boundary.
Downstream packages are released only when their code, packaged content, or compatibility requirements change.

The regular CI gate for package and documentation changes includes:

```bash
uv run ruff check .
uv run pyright
uv run pytest tests -m "not slow and not acceptance and not packaging and not package_tools and not complete and not wetlands and not public_data and not external_binary and not sairpico_binary and not model_runtime"
uv run pytest -m "acceptance and not complete"
uv run pytest -m "package_tools and not complete"
uv build --all-packages --no-sources --out-dir dist/packages
BIOIMAGEFLOW_PACKAGE_ARTIFACTS_DIR=dist/packages uv run pytest tests/unit/test_package_artifacts.py
uv run sphinx-build -W --keep-going docs/source docs/_build/html
```

The resource-dependent complete-test jobs run weekly to detect external drift and can also be selected manually before a relevant release.
They are useful release evidence, but deterministic unit, package-artifact, and documentation checks remain the required proof for ordinary package changes.

Wheels exclude package documentation, package tests, generated build outputs, and local caches.
Source distributions keep package docs and tests so release artifacts remain auditable without bloating installed wheels.
Release metadata must not expose broad extras that silently install all domain runtimes; users install the companion packages and isolated tool environments they actually need.
Publishing uses an explicitly dispatched coordinated GitHub Actions release set of annotated package tags at one reviewed SHA, after exact-head CI and affected runtime/artifact checks.
Current GitHub inspection found no required reviewers on the `pypi` environment and the tag ruleset disabled; no approval or tag protection is inferred.
The source-declared current cohort is Core `>=0.5.0,<0.6`; DataFrame packages also require BioImageFlow `>=0.9.0,<1`.
These are candidate metadata requirements, not a claim of published availability or completed exact-head/OS/runtime certification.
The campaign pauses releases through its accepted library milestone; standing release authorization does not replace technical gates.
All nine tool bounds must resolve and validate; the eight existing projects are the base release set, while first-time Phasor publisher admission is separate.
The release job requires successful exact-head normal CI, builds the selected distributions with workspace sources disabled, validates their artifacts, then publishes them in dependency order.
A manually selected affected resource suite is additionally blocking; unrelated models, downloads and binaries are not automatic release gates.

## Executable Admission and Cache Identity

Reusable cache lookup and execution consume one admitted executable capture rather than reconstructing source or package selectors after a cache hit.
Captured custom tools retain their admitted source content and canonical module/class identity; loading another single-file source with the same logical ID must not retarget the earlier definition.
Ordinary Direct tools remain supported and cacheable from their actual retained primary callbacks and represented code, defaults, closed literal state and same-source helper facts, with configured values and declarations supplied by the effective definition capture.
Managed source admission compiles captured bytes without executing imports and refuses proven resident/source primary, literal-global, helper or default mismatches before lookup; these controller-local bytecode comparisons are not cross-Python worker tokens.
Current ProcessingTask/result v4 uses the shared Core declaration projector: the loaded instance must match the requested portable IO contract before the scientific method, and the controller checks the successful digest before output acceptance.
This does not prove arbitrary constructor/module initializer effects, custom validator behavior or installed/transitive dependency bytes.
Retaining a callback prevents later method replacement from selecting another function, but does not freeze arbitrary module globals or closures after admission.
Mutable observational globals, dynamic initializers, opaque imported/native modules and installed transitive dependencies remain explicitly qualified; current paths, package labels and matching primary code alone are not complete executable-content proof.

## Tool Store Installation Readiness

`ToolRegistry.install_package()` reuses a completed installation only after admitting the requested canonical distribution/version, RECORD integrity and concrete recorded import member under the selected target root.
A package directory alone does not establish readiness.
Invalid import-package identifiers or PEP 440 versions are refused before filesystem writes or pip, while valid prerelease/local versions are supported.
The configured store may resolve to its chosen location, but a child link cannot redirect installation outside that store.
Incomplete, wrong-version or foreign occupied targets raise `ValueError` with the requested package, version and target; installation does not overwrite or delete their files.
Fresh installs run in unique owned staging and validate the resulting package before atomic publication without replacement.
A racing existing target, including an empty directory, raises `FileExistsError` and remains intact.
Pip failure preserves the detailed `RuntimeError` and original cause while cleaning only the attempt's staging.
Installation admission resolves the import member without executing its initializer; actual loading/construction are later gates, and arbitrary initializer side effects are not sandboxed.
These checks establish bounded requested-package readiness rather than universal dependency isolation or controller/worker installed-content equality.

Versioned controller/local loading uses `selected_installation` dependency authority: compatible preloaded distributions need actual membership/version evidence, and unequal or unknown ownership is refused without purging foreign modules.
Loading, initialization and tool callbacks use a scoped locked search-path context that restores caller paths; managed workers use `managed_runtime` authority in their own environment, without falling back to the entire controller store for dependencies.
Active imports and applicable requirements are checked before successful publication, preserving the primary tool exception on failure.
This is bounded dependency admission, not an environment solver, universal transitive-byte proof or sandbox for arbitrary trusted initializer/global effects.

## Package-Owned Documentation Contract

Each package owns its README, `docs/index.md`, tool pages, workflow pages, tests, fixtures, examples, and small runtime assets.
Every public tool and workflow should have package-local documentation and meaningful deterministic tests.
Schemas, metadata, real scientific execution and model/binary/public-data acceptance are separate evidence layers.
A dynamic DataFrame tool can be concrete without static Outputs; worker environment, IOModel outputs and RowConsumption admission apply to ProcessingTools.
Keep whole-batch isolated inference/training/aggregation with explicit consumed-input association; transport correlation does not require duplicated scientific results.
The main docs include first-party package docs through generated wrapper pages, but the source of truth remains in each package directory.

Custom packages can opt into local documentation builds with `[tool.bioimageflow.docs]` metadata in their `pyproject.toml`.
See the [custom tool package how-to guide](../how-to/custom_tool_package) for the package layout, documentation contract, and test expectations.
