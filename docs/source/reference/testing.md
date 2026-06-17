# Testing Reference

BioImageFlow uses two pytest levels so daily development stays fast while package maintainers can still run realistic validation with public data, real binaries, and optional model runtimes.

## Regular Tests

Regular tests are the default.
They must be deterministic, fast enough for agent development, and runnable without network access or private external binaries.

Regular tests should:

- use tiny generated fixtures or committed demo data;
- mock downloads, external binaries, model runtimes, and long-running tools;
- cover every public tool schema, successful execution path, output contract, and important failure mode;
- build and execute example workflows on the smallest useful fixture;
- write outputs only under pytest temporary directories.

Run the regular suite with:

```bash
uv run pytest
```

CI runs the deterministic default tier with slow tests excluded:

```bash
uv run pytest -m "not slow"
```

The GitLab CI regular-test matrix runs that command on Python 3.10, 3.11, and 3.12.

Package-local regular tests can be run with:

```bash
uv run pytest packages/bioimageflow-io-tools/tests
uv run pytest -m package_tools
```

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
- `slow`: takes materially longer than the regular package tests.

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
uv run pytest tests/priority_workflows -m "complete and wetlands" --run-complete
```

GitLab CI also defines manual or scheduled complete-test jobs that are separate from the required deterministic gates:

```bash
uv run pytest -m "complete and wetlands" --run-complete -rsx
uv run pytest -m "complete and public_data" --run-complete -rsx
uv run pytest -m "complete and external_binary" --run-complete -rsx
uv run pytest -m "complete and model_runtime" --run-complete -rsx
```

The Wetlands job is an umbrella portability selector.
Resource-specific jobs are focused reruns for triage, so scheduled complete pipelines may intentionally select some tests more than once.
Public-data cases selected by the umbrella Wetlands job still require `BIOIMAGEFLOW_ALLOW_PUBLIC_DOWNLOADS=1`; otherwise they skip with the same actionable reason as a local run.

Those jobs are allowed to fail in CI because they depend on service availability, downloads, optional model runtimes, or external binaries rather than only on deterministic product behavior.

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
Before broad finalization, run:

```bash
uv run ruff check .
uv run pyright
uv run pytest -m "not slow"
uv run pytest tests/unit/test_package_artifacts.py
uv build --all-packages --out-dir dist/packages
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
