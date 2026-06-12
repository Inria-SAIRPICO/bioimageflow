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
Resource markers are descriptive selectors; they are not permission to skip because a dependency is absent from the host environment.
Regular tests that only build graphs, check documentation, or use fake/mocked resources should not use `public_data`, `external_binary`, `sairpico_binary`, or `model_runtime`.

Complete tests are skipped unless explicitly enabled with `--run-complete`:

```bash
uv run pytest -m complete --run-complete
```

To run one complete Wetlands package or workflow slice:

```bash
uv run pytest packages/bioimageflow-sairpico-tools/tests -m "complete and wetlands" --run-complete
uv run pytest tests/priority_workflows -m "complete and wetlands" --run-complete
```

The remaining valid complete-test gates are:

- `--run-complete`, which opts into slower Wetlands environment creation and execution;
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
uv run pytest
uv run sphinx-build docs/source docs/_build/html
```

Complete tests are appropriate at the end of a long package or workflow iteration, or when a maintainer explicitly asks for them.
Agents should ask before triggering downloads, real binaries, or model runtimes unless the user has already approved those resources for the current task.

Review agents should verify that:

- regular tests do not require network access, private files, or real external binaries;
- complete tests are correctly marked and skipped by default;
- Wetlands complete tests validate environment creation and execution instead of host runtime availability;
- every public tool and example workflow has regular coverage and, when useful, complete coverage;
- expected results are asserted from files, tables, metadata, metrics, or other observable outputs.
