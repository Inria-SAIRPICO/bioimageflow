# Testing Reference

BioImageFlow uses multiple pytest levels so daily development stays fast while package maintainers can still run deterministic high-level validation and realistic validation with public data, real binaries, and optional model runtimes.

## Local Default Tests

The default local pytest run executes deterministic tests and skips only the complete/resource tiers that require `--run-complete`.
It is useful before broad finalization, but it is broader than the CI fast matrix because it still includes dedicated markers such as `acceptance`, `packaging`, `parsl`, and `slow`.

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

Portable viewer grammar, metadata projections, package TOML parsing, upload preflight and import-direction checks use finite ordinary fixtures in the fast tier.
The Core source type-marker check is separate from the packaging tier's actual wheel/source-distribution closure; source presence alone does not certify built artifacts.

The ordinary **CI** workflow (`ci.yml`) runs the backend-neutral fast selector with deterministic non-fast and complete/resource tiers excluded, and applies these fixed distributed-path exclusions to local unit, integration, compatibility, acceptance and package-tool selections:

```bash
export LOCAL_LIBRARY_PYTEST_ARGS="--ignore=tests/unit/parsl --ignore=tests/unit/cluster --ignore=tests/unit/launcher --ignore=tests/integration/parsl --ignore=tests/integration/launcher --ignore=tests/unit/test_distributed_contract.py"
```

These exclusions also cover unmarked distributed tests; shared DTO, serialization and static import guards outside those paths remain local coverage.
Using that selection, run the local fast tests with:

```bash
uv run pytest tests $LOCAL_LIBRARY_PYTEST_ARGS -m "not slow and not acceptance and not packaging and not package_tools and not complete and not wetlands and not public_data and not external_binary and not sairpico_binary and not model_runtime and not parsl"
```

GitHub Actions runs `tests/unit` and `tests/integration` as independent jobs on Python 3.10 and 3.12 so failures arrive sooner and can be rerun by concern.
Python 3.11 runs the backend-neutral `compat` smoke selector on every pipeline.

Using the same fixed exclusions above, run the Python-version compatibility smoke selector with:

```bash
uv run pytest tests $LOCAL_LIBRARY_PYTEST_ARGS -m "compat and not slow and not acceptance and not packaging and not package_tools and not complete and not wetlands and not public_data and not external_binary and not sairpico_binary and not model_runtime and not parsl"
```

Shared-array controls use controller-owned temporary NPY/mmap files on supported Python/OS runtimes and remain included in the required fast matrix.
They no longer require POSIX shared-memory segments; a real filesystem/mapping admission failure must be reported separately rather than silently excluding the marker or crediting unrun lifetime coverage.

Package-local regular tests are a separate deterministic required tier and can be run with:

```bash
uv run pytest packages/bioimageflow-io-tools/tests
uv run pytest -m "package_tools and not complete"
```

## Local and Distributed CI

Local and distributed coverage have separate visible workflows on main pushes, pull requests and manual dispatches.
The ordinary **CI** workflow always uses the fixed local exclusions above; **Distributed CI** (`distributed.yml`) retains the complementary unmarked distributed unit/integration selections and the existing real Parsl runtime jobs.
Both workflows report their actual failures, with independent concurrency and no suppressed errors or branch-specific exceptions.
The split preserves selected coverage; it does not repair or certify distributed features, and earlier failed combined CI results remain failed.
Quality, normal package/Core artifacts and documentation remain required local gates.
Only successful ordinary push/pull-request `ci.yml` at the exact release SHA qualifies publication; a distributed or manual capability run does not replace that authority.
The separate manually dispatched local WorkerPool job executes four explicit owner files with editable/source Core; it does not certify source-disabled installed library dispatch.
The normal installed Core matrix separately checks public contracts and child/view/owner lifetime on Python 3.9 and 3.12 on Linux and Windows.
Configured matrix coverage is distinct from successful run evidence, and manual capability CI never unlocks release publication.

## Distributed and Parsl Tests

The `parsl` marker is reserved for tests that execute the real optional Parsl runtime.
Fake DFK and future tests remain ordinary unit tests, but distributed-owned unmarked tests run in the separate **Distributed CI** workflow rather than becoming local coverage merely because no real executor starts.
Its complementary fast unit jobs use `tests/unit/parsl`, `tests/unit/cluster`, `tests/unit/launcher` and `tests/unit/test_distributed_contract.py`; fast integration jobs use `tests/integration/parsl` and `tests/integration/launcher`.
Both complementary jobs retain Python 3.10 and 3.12 and the same fast marker selector above, without local ignore arguments; the marked runtime selectors below are disjoint from them.

Fast real-runtime tests use local thread executors and run on Python 3.10 and 3.12 in the separate distributed matrix:

```bash
uv run pytest tests -m "parsl and not slow"
```

The `slow` Parsl tier includes process-isolated executor cases and explicitly configured cluster smoke.
The distributed workflow’s Python 3.11 job retains that selector; local process isolation needs no external scheduler, while cluster smoke skips unless `BIOIMAGEFLOW_PSIJ_SMOKE_CONFIG` supplies its maintainer-owned site configuration:

```bash
uv run pytest tests -m "parsl and slow"
```

## Deterministic Non-Fast Tests

Deterministic non-fast tests are required coverage, but they are not part of every Python-version matrix job.
Use these markers when coverage is valuable but too broad or artifact-oriented for the fast loop:

- `acceptance`: high-level workflow or example coverage that executes deterministic scenarios;
- `compat`: deterministic Python-version compatibility smoke coverage;
- `parsl`: tests that execute the real optional Parsl runtime in the dedicated runtime jobs;
- `packaging`: build artifact, wheel, sdist, or package metadata artifact checks;
- `package_tools`: package-local deterministic coverage that is required in a separate CI job;
- `shared_memory`: deterministic scoped numeric file/mmap lifetime tests;
- `slow`: deterministic or external tests excluded from the fast development loop.

Run deterministic acceptance coverage with:

```bash
uv run pytest -m "acceptance and not complete"
```

Run package artifact checks with:

```bash
uv run pytest tests/unit/test_package_artifacts.py
```

The artifact contract verifies that Parsl is declared only by the orchestrator's `parsl` extra and that importing the base wheel never imports the optional runtime.

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
- `cluster_smoke`: requires a maintainer-configured OpenSSH, PSI/J, and scheduler site;
- `slow`: takes materially longer than the fast package tests.

Reserve resource markers for tests that actually require those resources.
External resource markers are descriptive selectors, and the external markers listed below also keep service-dependent tests out of the default pytest run.
They are not permission to skip because a dependency is absent from the host environment after the external tier has been enabled.
Regular tests that only build graphs, check documentation, or use fake/mocked resources should not use `public_data`, `external_binary`, `sairpico_binary`, `model_runtime`, or `cluster_smoke`.

Tests marked `complete`, `wetlands`, `public_data`, `external_binary`, `sairpico_binary`, `model_runtime`, or `cluster_smoke` are skipped unless explicitly enabled with `--run-complete`:

```bash
uv run pytest -m complete --run-complete
```

To run one complete Wetlands package or workflow slice:

```bash
uv run pytest packages/bioimageflow-sairpico-tools/tests -m "complete and wetlands" --run-complete
uv run pytest packages/bioimageflow-common-tools/tests packages/bioimageflow-segmentation-tools/tests -m "complete and wetlands" --run-complete
```

The **Complete validation** GitHub Actions workflow separates deterministic release validation from resource-dependent complete tests:

- `wetlands`, `public-data`, `external-binaries`, and `model-runtimes` can be selected individually through a manual workflow dispatch;
- the four resource-dependent suites run every Monday at 03:00 UTC to detect environment, download, binary, service, and model drift even when the repository has not changed;
- deterministic coverage belongs to the ordinary exact-commit CI workflow rather than a duplicate Complete job.

For a changed runtime, select `suite=model-runtimes` and one fixed `runtime_gate`: `stardist`, `instanseg`, `nagini-api`, or `laptrack`.
The default `runtime_gate=all` preserves the existing full model-runtime selector and weekly schedule.
Selection is validated before any resource job starts, and separate suite/gate concurrency keys keep independent targeted dispatches from replacing one another.
The three direct runtimes use isolated interpreters and exact dependencies captured from the selected tool's public `EnvironmentSpec`; StarDist retains its managed worker recipe instead of treating its host interpreter as runtime evidence.
Targeted gates require the exact selected JUnit case with no failures, errors, or skips, and the external-binary gate requires all eight real SAIRPICO cases.
API/import coverage for `nagini-api` is explicitly narrower than native model inference.

The corresponding local commands are:

```bash
uv run pytest -m "complete and wetlands" --run-complete -rsx
uv run pytest -m "complete and public_data" --run-complete -rsx
uv run pytest -m "complete and external_binary" --run-complete -rsx
uv run pytest -m "complete and model_runtime" --run-complete -rsx
uv run pytest tests/integration/parsl/test_cluster_smoke.py --run-complete -rsx
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
The ownership map is stored in `tests/ownership.toml`; declared source owners take precedence over generic docs and example routing.
At edit stage an existing collected `test_*.py` selects that file, prose documentation selects strict Sphinx, and shared support or configuration retains its broader guards.
Unknown paths and merge-stage checks fail open to the broader suites, and the helper never replaces CI gates.
See :doc:`platform_development` for source ownership, module-size limits, dependency boundaries, and the backend seam.

Before broad local finalization, use the fixed `LOCAL_LIBRARY_PYTEST_ARGS` selection above and run:

```bash
uv run ruff check .
uv run pyright
uv run python scripts/check_file_sizes.py
uv run python scripts/check_import_boundaries.py
uv run pytest tests/unit $LOCAL_LIBRARY_PYTEST_ARGS -m "not slow and not acceptance and not packaging and not package_tools and not complete and not wetlands and not public_data and not external_binary and not sairpico_binary and not model_runtime and not parsl"
uv run pytest tests/integration $LOCAL_LIBRARY_PYTEST_ARGS -m "not slow and not acceptance and not packaging and not package_tools and not complete and not wetlands and not public_data and not external_binary and not sairpico_binary and not model_runtime and not parsl"
uv run pytest $LOCAL_LIBRARY_PYTEST_ARGS -m "acceptance and not complete"
uv run pytest $LOCAL_LIBRARY_PYTEST_ARGS -m "package_tools and not complete"
uv run pytest tests/unit/test_package_artifacts.py
uv build --all-packages --no-sources --out-dir dist/packages
BIOIMAGEFLOW_PACKAGE_ARTIFACTS_DIR=dist/packages uv run pytest tests/unit/test_package_artifacts.py
uv run sphinx-build -W --keep-going docs/source docs/_build/html
```

Pyright is an implementation gate.
The checked configuration includes package implementation code and excludes test modules because root tests still contain dynamic negative-test and pandas-stub idioms that are not part of the product type contract.

Complete tests are appropriate at the end of a long package or workflow iteration, or when a maintainer explicitly asks for them.
Agents should ask before triggering downloads, real binaries, or model runtimes unless the user has already approved those resources for the current task.

Review agents should verify that:

- regular tests do not require network access, private files, or real external binaries;
- complete tests are correctly marked and skipped by default;
- Wetlands complete tests validate environment creation and execution instead of host runtime availability;
- every public tool and example workflow has regular coverage and, when useful, complete coverage;
- expected results are asserted from files, tables, metadata, metrics, or other observable outputs.
