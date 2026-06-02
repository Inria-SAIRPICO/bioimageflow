# Testing Reference

BioImageFlow uses two pytest levels so daily development stays fast while
package maintainers can still run realistic validation with public data, real
binaries, and optional model runtimes.

## Regular Tests

Regular tests are the default. They must be deterministic, fast enough for
agent development, and runnable without network access or private external
binaries.

Regular tests should:

- use tiny generated fixtures or committed demo data;
- mock downloads, external binaries, model runtimes, and long-running tools;
- cover every public tool schema, successful execution path, output contract,
  and important failure mode;
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

Complete tests are opt-in validation for realistic scenarios that are too
expensive or environment-dependent for every development pass. They are for
real binaries, public datasets, model downloads, larger fixtures, and longer
end-to-end workflows.

Complete tests must be marked with `@pytest.mark.complete` and one or more
specific resource markers when relevant:

- `public_data`: downloads or uses public datasets;
- `external_binary`: requires a non-Python command-line program;
- `sairpico_binary`: requires real SAIRPICO binaries;
- `model_runtime`: requires optional model runtimes or model downloads;
- `slow`: takes materially longer than the regular package tests.

Reserve resource markers for tests that actually require those resources.
Regular tests that only build graphs, check documentation, or use fake/mocked
resources should not use `public_data`, `external_binary`, `sairpico_binary`,
or `model_runtime`.

Complete tests are skipped unless explicitly enabled:

```bash
uv run pytest -m complete --run-complete
```

To run one complete package or workflow slice:

```bash
uv run pytest packages/bioimageflow-sairpico-tools/tests -m complete --run-complete
uv run pytest tests/priority_workflows -m complete --run-complete
```

If a complete test needs a local binary or dataset path, use an environment
variable with a clear name and skip with an actionable reason when it is not
set. A developer who enables complete tests should be able to tell immediately
which resource is missing.

Current complete workflow resource gates:

- SAIRPICO workflow and package tests require the needed `simg*` or
  `hotSpotDetection` commands on `PATH`.
- Atlas workflow tests require `atlas` on `PATH`.
- Cellpose/StarDist workflow tests require the optional Python modules to be
  importable in the active environment.
- Public CIL FISH workflow execution requires
  `BIOIMAGEFLOW_ALLOW_PUBLIC_DOWNLOADS=1` in addition to `--run-complete`.

Current package complete resource gates:

- SAIRPICO package binary tests require the corresponding SAIRPICO command on
  `PATH`.
- Common-tools `ConnectedComponents` complete coverage requires `SimpleITK`.
- Segmentation package model-runtime tests require `cellpose`, `stardist`, or
  `csbdeep` depending on the tool under test.

## Test Data

Use the smallest data that proves the behavior.

- Synthetic fixtures are preferred for regular tests.
- Tiny committed fixtures are acceptable when they represent a format feature
  that is hard to generate in the test.
- Public datasets belong in complete tests unless the downloaded artifact is
  tiny, stable, and cached by the test.
- Package-local `tests/data/README.md` files should record fixture provenance,
  license, expected outputs, and regeneration notes.
- Never commit private microscopy data, caches, model weights, or generated
  workflow outputs.

## Agent Workflow

Agents should run relevant regular tests while developing. Before broad
finalization, run:

```bash
uv run ruff check .
uv run pytest
uv run sphinx-build docs/source docs/_build/html
```

Complete tests are appropriate at the end of a long package or workflow
iteration, or when a maintainer explicitly asks for them. Agents should ask
before triggering downloads, real binaries, or model runtimes unless the user
has already approved those resources for the current task.

Review agents should verify that:

- regular tests do not require network access, private files, or real external
  binaries;
- complete tests are correctly marked and skipped by default;
- every public tool and example workflow has regular coverage and, when useful,
  complete coverage;
- expected results are asserted from files, tables, metadata, metrics, or other
  observable outputs.
