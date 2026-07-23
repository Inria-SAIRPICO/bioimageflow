# Testing Reference

BioImageFlow uses multiple pytest levels so daily development stays fast while package maintainers can still run deterministic high-level validation and realistic validation with public data, real binaries, and optional model runtimes.

## Local Default Tests

The default local pytest run executes deterministic tests and skips only the complete/resource tiers that require `--run-complete`.
It is useful before broad finalization, but it is broader than the CI fast matrix because it still includes deterministic non-fast markers such as `acceptance`, `packaging`, and `slow`.

Run the default local suite with:

```bash
uv run pytest
```

## Fast Tests

Fast tests are the required CI development loop.
They must be deterministic, fast enough for agent development, and runnable without network access or private external binaries.

Fast tests should:

- use tiny generated fixtures or committed demo data;
- mock downloads, external binaries, model runtimes, and long-running tools;
- cover public tool schemas, core mechanisms, successful execution paths, output contracts, and important failure modes;
- write outputs only under pytest temporary directories.

CI runs the required fast selector with deterministic non-fast and complete/resource tiers excluded:

```bash
uv run pytest tests -m "not slow and not acceptance and not packaging and not package_tools and not complete and not wetlands and not public_data and not external_binary and not sairpico_binary and not model_runtime"
```

GitHub Actions runs `tests/unit` and `tests/integration` as independent jobs on Python 3.10 and 3.12 so failures arrive sooner and can be rerun by concern.
Python 3.11 runs the `compat` smoke selector on every pipeline, while full deterministic Python 3.11 validation is manually available before tagging and rerun by the release workflow as a required publication gate.

Run the Python-version compatibility smoke selector with:

```bash
uv run pytest tests -m "compat and not slow and not acceptance and not packaging and not package_tools and not complete and not wetlands and not public_data and not external_binary and not sairpico_binary and not model_runtime"
```

For restricted sandboxes that cannot create POSIX shared-memory segments, agents may run a partial local fast loop with shared-memory tests excluded:

```bash
uv run pytest tests -m "not slow and not acceptance and not packaging and not package_tools and not complete and not wetlands and not public_data and not external_binary and not sairpico_binary and not model_runtime and not shared_memory"
```

That command is only for local sandbox triage.
It does not replace the required CI fast matrix, where `shared_memory` remains included.

Package-local regular tests are a separate deterministic required tier and can be run with:

```bash
uv run pytest packages/bioimageflow-io-tools/tests
uv run pytest -m "package_tools and not complete"
```

## Deterministic Non-Fast Tests

Deterministic non-fast tests are required coverage, but they are not part of every Python-version matrix job.
Use these markers when coverage is valuable but too broad or artifact-oriented for the fast loop:

- `acceptance`: high-level workflow or example coverage that executes deterministic scenarios;
- `compat`: deterministic Python-version compatibility smoke coverage;
- `packaging`: build artifact, wheel, sdist, or package metadata artifact checks;
- `package_tools`: package-local deterministic coverage that is required in a separate CI job;
- `shared_memory`: deterministic tests requiring POSIX/shared-memory platform support;
- `slow`: deterministic or external tests excluded from the fast development loop.

Run deterministic acceptance coverage with:

```bash
uv run pytest -m "acceptance and not complete"
```

Run package artifact checks with:

```bash
uv run pytest tests/unit/test_package_artifacts.py
```

To validate an existing package artifact directory instead of building inside the test fixture, point the test at the prebuilt output:

```bash
uv build --all-packages --no-sources --out-dir dist/packages
BIOIMAGEFLOW_PACKAGE_ARTIFACTS_DIR=dist/packages uv run pytest tests/unit/test_package_artifacts.py
```

Run deterministic package-tool coverage with:

```bash
uv run pytest -m "package_tools and not complete"
```

GitHub Actions runs deterministic acceptance, package-tool, and packaging commands in explicit jobs, separate from the Python-version fast matrix.

## Complete Tests

Complete tests are opt-in validation for realistic scenarios that are too expensive or environment-dependent for every development pass.
They validate BioImageFlow's portability contract: workflows and tools must create and execute through their declared Wetlands-managed environments instead of relying on optional modules or binaries in the host Python environment.
Missing optional runtimes on the host machine should not skip a Wetlands complete test.
If a tool's declared `EnvironmentSpec` cannot produce a working Wetlands environment, the complete test should fail because the portable runtime contract is broken.

Complete tests that execute real tools must be marked with `@pytest.mark.complete` and `@pytest.mark.wetlands`.
Add one or more specific resource markers when relevant:

- `public_data`: downloads or uses public datasets;
- `external_binary`: requires a non-Python command-line program;
- `sairpico_binary`: requires real SAIRPICO binaries;
- `model_runtime`: requires optional model runtimes or model downloads;
- `slow`: takes materially longer than the fast package tests.

Reserve resource markers for tests that actually require those resources.
External resource markers are descriptive selectors, and the external markers listed below also keep service-dependent tests out of the default pytest run.
They are not permission to skip because a dependency is absent from the host environment after the external tier has been enabled.
Regular tests that only build graphs, check documentation, or use fake/mocked resources should not use `public_data`, `external_binary`, `sairpico_binary`, or `model_runtime`.

Tests marked `complete`, `wetlands`, `public_data`, `external_binary`, `sairpico_binary`, or `model_runtime` are skipped unless explicitly enabled with `--run-complete`:

```bash
uv run pytest -m complete --run-complete
```

To run one complete Wetlands package or workflow slice:

```bash
uv run pytest packages/bioimageflow-sairpico-tools/tests -m "complete and wetlands" --run-complete
uv run pytest packages/bioimageflow-common-tools/tests packages/bioimageflow-segmentation-tools/tests -m "complete and wetlands" --run-complete
```

The **Complete validation** GitHub Actions workflow separates deterministic release validation from resource-dependent complete tests:

- `release-validation` is manual-only and provides full deterministic Python 3.11 evidence before a release tag is created;
- `wetlands`, `public-data`, `external-binaries`, and `model-runtimes` can be selected individually through a manual workflow dispatch;
- the four resource-dependent suites run every Monday at 03:00 UTC to detect environment, download, binary, service, and model drift even when the repository has not changed;
- the weekly schedule does not repeat `release-validation`, because equivalent deterministic coverage already runs for repository changes and is rerun by the release workflow.

The corresponding local commands are:

```bash
uv run pytest -m "complete and wetlands" --run-complete -rsx
uv run pytest -m "complete and public_data" --run-complete -rsx
uv run pytest -m "complete and external_binary" --run-complete -rsx
uv run pytest -m "complete and model_runtime" --run-complete -rsx
```

The Wetlands job is an umbrella portability selector.
Resource-specific jobs are focused reruns for triage, so weekly complete workflows may intentionally select some tests more than once.
Public-data cases selected by the umbrella Wetlands job still require `BIOIMAGEFLOW_ALLOW_PUBLIC_DOWNLOADS=1`; otherwise they skip with the same actionable reason as a local run.

Resource-dependent jobs use `continue-on-error` only during scheduled monitoring because they depend on service availability, downloads, optional model runtimes, or external binaries rather than only on deterministic product behavior.
A manually selected resource suite is blocking and must pass when used as pre-release evidence.

The remaining valid complete-test gates are:

- `--run-complete`, which opts into slower Wetlands environment creation, external/service-dependent resources, and execution;
- `BIOIMAGEFLOW_ALLOW_PUBLIC_DOWNLOADS=1`, for tests that download public datasets.

Public-data tests should skip with an actionable reason when `BIOIMAGEFLOW_ALLOW_PUBLIC_DOWNLOADS=1` is not set.
Wetlands complete tests should not use host `PATH` checks, host Python import checks, or direct host-runtime `process_row()` calls for tools whose portability depends on a declared environment.

## Test Data

Use the smallest data that proves the behavior.

- Synthetic fixtures are preferred for regular tests.
- Tiny committed fixtures are acceptable when they represent a format feature that is hard to generate in the test.
- Public datasets belong in complete tests unless the downloaded artifact is tiny, stable, and cached by the test.
- Package-local `tests/data/README.md` files should record fixture provenance, license, expected outputs, and regeneration notes.
- Never commit private microscopy data, caches, model weights, or generated workflow outputs.

## Agent Workflow

Agents should run relevant regular tests while developing.
For the smallest edit-loop validation mapped to changed platform areas, run:

```bash
git diff --name-only | uv run python scripts/affected_tests.py --stdin
```

For suite-level validation before committing, add `--stage precommit`.
For the complete deterministic fast merge gate, add `--stage merge`.
The ownership map is stored in `tests/ownership.toml`, unknown paths fail open to the full fast suite, and the helper never replaces CI gates.
See :doc:`platform_development` for source ownership, module-size limits, dependency boundaries, and the backend seam.

Before broad finalization, run:

```bash
uv run ruff check .
uv run pyright
uv run python scripts/check_file_sizes.py
uv run python scripts/check_import_boundaries.py
uv run pytest tests/unit -m "not slow and not acceptance and not packaging and not package_tools and not complete and not wetlands and not public_data and not external_binary and not sairpico_binary and not model_runtime"
uv run pytest tests/integration -m "not slow and not acceptance and not packaging and not package_tools and not complete and not wetlands and not public_data and not external_binary and not sairpico_binary and not model_runtime"
uv run pytest -m "acceptance and not complete"
uv run pytest -m "package_tools and not complete"
uv run pytest tests/unit/test_package_artifacts.py
uv build --all-packages --no-sources --out-dir dist/packages
BIOIMAGEFLOW_PACKAGE_ARTIFACTS_DIR=dist/packages uv run pytest tests/unit/test_package_artifacts.py
uv run sphinx-build -W --keep-going docs/source docs/_build/html
```

Pyright is an implementation gate in this phase.
The checked configuration includes package implementation code and excludes test modules because root tests still contain dynamic negative-test and pandas-stub idioms that are not part of the product type contract.

Complete tests are appropriate at the end of a long package or workflow iteration, or when a maintainer explicitly asks for them.
Agents should ask before triggering downloads, real binaries, or model runtimes unless the user has already approved those resources for the current task.

Review agents should verify that:

- regular tests do not require network access, private files, or real external binaries;
- complete tests are correctly marked and skipped by default;
- Wetlands complete tests validate environment creation and execution instead of host runtime availability;
- every public tool and example workflow has regular coverage and, when useful, complete coverage;
- expected results are asserted from files, tables, metadata, metrics, or other observable outputs.
