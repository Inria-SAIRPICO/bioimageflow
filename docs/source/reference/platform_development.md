# Platform Development

This reference describes the repository structure and feedback loops intended to keep large execution-engine changes fast and reviewable.

## Orchestrator structure

The public imports remain stable, but the main orchestration domains are packages of focused modules:

```text
bioimageflow/
├── backends.py          # Small processing-backend protocol and direct/Wetlands adapters
├── engine/              # Scheduling, planning, dispatch, graph traversal, cache runtime
├── workflow/            # Workflow model, loading, runtime, interfaces, serialization
├── storage/             # Immutable records, identities, manifests, run/output views
├── cache/               # Cache identities, metadata, dataframe and asset publication
├── validation/          # Schemas, serialization, constants, validation value objects
└── events.py            # Backend-neutral progress event values
```

The package `__init__.py` modules define the public import surface.
Callers import public names from `bioimageflow`, `bioimageflow.engine`, `bioimageflow.workflow`, `bioimageflow.storage`, `bioimageflow.cache`, and `bioimageflow.validation`.
New internal code should import from the focused owner module when doing so does not introduce a dependency cycle.

The launcher package owns submitted execution and laptop-to-cluster transport.
Its ``cluster_protocol`` module owns the bounded JSON envelope, ``cluster_bundle`` owns laptop packaging, ``cluster_upload`` and ``cluster_submit`` own one-shot server mutations, ``ssh`` owns shell-free system OpenSSH/SFTP invocation, and ``remote_run`` and the result modules own remote observation and atomic local materialization.
The installed ``bioimageflow-cluster-agent`` command is a thin standard-input/standard-output adapter over those modules.

The enforced dependency direction is storage → cache → engine → workflow.
Storage must not import cache, engine, backends, or workflow; cache must not import engine, backends, or workflow; engine must not import workflow.
The worker-safe `bioimageflow-core` package must not import pandas, pydantic, or the orchestrator at module import time.

## Execution backend seam

`ProcessingBackend` is intentionally smaller than the scheduler.
The scheduler owns DAG semantics, input resolution, cache selection/publication, progress, cancellation, and deterministic error behavior.
A backend prepares an uncached processing node, dispatches one immutable `ProcessingDispatch`, and releases execution- or engine-owned resources.

Direct, Wetlands, and Parsl implement this same contract.
Backend-specific task policy and transport values should remain backend-neutral until dispatch, and observable behavior shared by all engines belongs in the scheduler or another shared module.

## Fast edit loop

`tests/ownership.toml` maps each orchestrator area to focused tests and broader precommit suites.
Use changed paths to print the smallest useful commands:

```bash
git diff --name-only | uv run python scripts/affected_tests.py --stdin
```

Use suite-level validation after a coherent change:

```bash
git diff --name-only | uv run python scripts/affected_tests.py --stdin --stage precommit
```

Use the complete deterministic fast selector before merge:

```bash
git diff --name-only | uv run python scripts/affected_tests.py --stdin --stage merge
```

Unknown source paths fail open to the full fast suite.
The selector is advisory during editing; CI remains authoritative and runs on every pull request without path-based skipping.

Tests are grouped under `tests/unit` and `tests/integration`, with reusable tools and fixtures under `tests/testkit`.
Large behavior areas use test packages so a developer can run a single concern, such as `tests/integration/runtime_cache/test_dataframe_cache.py`, without collecting unrelated cases.

## Guardrails

Run the structural checks directly with:

```bash
uv run python scripts/check_file_sizes.py
uv run python scripts/check_import_boundaries.py
uv run pytest tests/unit/test_development_workflow.py
```

Orchestrator modules have an 800-line hard ceiling and should normally remain much smaller.
Actual test modules have a 500-line hard ceiling.
Shared test helpers and pytest configuration have a 700-line hard ceiling.
Split by responsibility before raising a limit.

When adding an orchestrator source module, add it to exactly the relevant source area in `tests/ownership.toml`.
When adding a dependency between platform layers, update the design instead of weakening an import boundary unless the architecture contract itself has deliberately changed.

## CI topology

Every pull request runs independent backend-neutral unit and direct-integration jobs on Python 3.10 and 3.12, plus a Python 3.11 compatibility smoke job.
Real Parsl coverage runs in a dedicated fast Python 3.10/3.12 matrix and a required process-isolation Python 3.11 job.
Quality, deterministic acceptance/package tests, package builds, and documentation are separate jobs.
This topology shortens feedback time while preserving unconditional coverage.
