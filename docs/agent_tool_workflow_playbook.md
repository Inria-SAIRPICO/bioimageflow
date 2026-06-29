# Agent Tool and Workflow Playbook

Use this checklist when adding or reviewing BioImageFlow tool packages,
workflow examples, or authoring documentation.

## Package Checklist

- Package boundary is explicit: tools, workflows, tests, fixtures, examples,
  small assets, and docs live with the owning package.
- Examples import from the new package name only.
- No backward compatibility shim is added unless a maintainer explicitly asks.
- Runtime assets are small and committed only when required for behavior.
- Heavy models, datasets, binaries, and generated outputs are downloaded,
  declared as dependencies, mocked in tests, or produced at runtime.

## Tool Checklist

- `ProcessingTool` is used for row-wise or batched worker execution.
- `DataFrameTool` is used only for main-process dataframe operations.
- `ProcessingTool` modules import only the standard library and
  `bioimageflow-core` at module import time.
- Tool-specific dependencies are imported inside `process_row` or
  `process_batch`.
- `Inputs` and `Outputs` use serializable standard-library types and
  BioImageFlow metadata.
- Tests cover schema serialization, binding validation, one tiny successful
  execution, output template resolution, returned `Outputs`, and expected
  failure paths.
- Regular and complete test tiers follow the central policy in
  `docs/source/reference/testing.md`.

## Workflow Checklist

- Workflow inputs and outputs are public, documented, and stable.
- Validation data is public, permissively licensed, committed as a tiny
  fixture, or generated synthetically with deterministic seeds.
- Tests build the graph, validate the public contract, run the smallest useful
  end-to-end case, and assert observable outputs.
- Serialization round-trips are covered when the workflow is meant to be
  exported or loaded.
- Examples avoid local absolute paths and private datasets.

## Docs Checklist

- Authoring docs explain package boundaries, process boundaries, imports,
  tests, and validation data.
- Code snippets use current packages and public APIs.
- Images live under `docs/images/` only when they clarify expected behavior,
  UI state, or workflow output.
- Images are small PNG/WebP files from public, permissively licensed, or
  synthetic data.
- Docs do not commit cache directories, private microscopy data, or generated
  artifacts.

## Reviewer Checklist

- Confirm the change stays inside the claimed ownership area.
- Confirm every new or changed public tool/workflow has tests. Public tools
  are classes re-exported from a package `__init__.py`, classes documented in
  a package README/Sphinx page, or classes used by an example workflow.
- Confirm `ProcessingTool` imports respect the worker process boundary.
- Confirm workflow-local `tools/` directories either omit `__init__.py` or keep it empty/docstring-only. If an `__init__.py` is present, it must not use eager barrel exports such as `from .some_tool import SomeTool`.
- Confirm simple source, download, path, CSV/table, and file utility `ProcessingTool`s use `GENERAL_ENV` unless they need a specialized dependency outside numpy, scipy, scikit-image, imageio, tifffile, Pillow, or the Python standard library.
- Confirm examples use new package imports and no migration shim was added.
- Confirm package ownership by checking the package README, its
  `pyproject.toml`, and the changed imports in example workflows.
- Run the specific tests added or changed by the branch, plus the closest
  existing package tests under `tests/unit/` and `tests/integration/`.
- Run `uv run ruff check <changed package/test paths>`.
- Run `uv run sphinx-build docs/source docs/_build/html` when docs changed,
  or document why it could not run.
- For workflow branches, run or review the smallest fixture-backed workflow
  execution test and confirm slow/public-data tests are marked.
- Confirm complete tests use `@pytest.mark.complete`, are skipped by default,
  and document any required public data, binary, or model runtime resource.
