# bioimageflow-core

Worker-safe core APIs for BioImageFlow tools.

This package contains `ProcessingTool`, `IOModel`, `Arguments`, `ExecutionContext`, `Template`, `EnvironmentSpec`, image type metadata, portable `ViewerSpec`/`NapariRequirement` metadata, and shared-memory helpers.
It is installed in the main process and in tool worker environments.
It declares NumPy because shared-memory helpers expose NumPy array views at runtime.
It declares `packaging` to validate portable PEP 440 viewer-package constraints without importing napari or plugin discovery code.
The `bioimageflow` orchestrator injects a pinned published `bioimageflow-core` package into Wetlands worker environments by default.
During source development, set `BIOIMAGEFLOW_CORE_SOURCE` to this project directory before creating worker environments to validate and inject it as an editable dependency.

Install:

```bash
pip install bioimageflow-core
```

For workspace development, use the repository root:

```bash
uv sync
uv run pytest packages/bioimageflow-core tests/unit/test_core_package_metadata.py
```

Core supports scientific worker Python >=3.9; the orchestrator requires Python >=3.10.
These floors do not certify every OS, model, binary or package recipe.
Portable IOModel declarations and GUI schema strings are distinct from runtime scientific values: current typed transport supports numeric NumPy arrays and scalars without admitting arbitrary third-party annotation classes or picklable objects.
IOModel enforces structural fields; semantic value validation belongs to the orchestrator.
Omitted mutable defaults are detached per instance, while explicitly supplied values preserve caller identity.
`IOModel.capture_defaults()` and `bioimageflow_core.defaults.snapshot_value` provide owned semantic copies for admitted definitions; scoped references retain their original local owner, and runtime managers/locks are not definition values.
Postponed and inherited annotations retain their metadata without importing unavailable declaration modules.

Numeric creation/mapping helpers reject object-containing NumPy dtypes before backing allocation or mapping.
Numeric data and Path/str image dispatch retain their essential behavior; explicit owners and live reader/worker grants control file lifetime.

Current processing transport uses `ProcessingTask`, `RowInvocation`, `ProcessingTaskResult`, and `RowResult` with explicit task/result v2 envelopes.
The public task/result codecs use the same typed grammar for arguments and outputs: primitive leaves, explicit dictionary/list/tuple nodes, Paths, SharedArray references, NumPy arrays, and dtype-preserving numeric NumPy scalars.
Literal dictionaries cannot collide with typed descriptors, and SharedArray decoding never allocates or attaches memory.
Unsupported objects, cyclic containers, object-containing dtypes, and malformed descriptors are refused; picklability alone is not sufficient.
SharedArray references are host-local, so distributed callers such as Parsl must refuse them even though local Wetlands workers can transport the reference.

### Shared-array ownership

SharedArray uses scoped numeric NPY2 file-backed mmap storage, with one initial copy and zero-copy mapped views thereafter.
Allocate under an explicit `SharedMemoryContext.activate()`; helper/context exit only unbinds.
References carry a strong local owner excluded from equality and wire; pure typed decoding is attachment-free, and workers borrow explicit task/input descriptors.
Controller close/release refuses new access while mapped views and admitted workers retain backing; `CleanupStatus` reports pending readers/grants/files/errors until physical drain.
Workflow/engine completion never auto-closes returned references; no resource tracker or private unregister is involved.
Accepted and borrowed scientific inputs are immutable to consumers by contract; mutable processing creates a separate work/output value.

The accepted lifecycle target also requires public owner/status access and exact result/group release, so callers can release a discarded result without closing unrelated groups.
Normal reusable worker pools can retain physical grants; idle or quota-pressure retirement must drain the owning pool before reclaiming backing, while live mapped views remain valid.
If safe retirement cannot reclaim enough space, finite allocation budgets must refuse clearly.
The exact result/group API and conformance are assessed in the ordered test and causal-code review; these requirements are not a claim that an unspecified method already exists.
See the [library specifications](../../docs/source/specs.md) for the declaration, typed-value and resource-owner contracts.


Worker tool source admission
----------------------------

A standalone source-file origin hashes and executes one captured byte sequence, without reopening the source through importlib or using cached bytecode.
Worker loading checks the selected module and the tool class's defining module against the selected source file, package root or installed distribution members.
A cached version-scoped package from a different store root is refused rather than substituted or evicted.
Failed initialization or construction does not publish a tool instance; only newly admitted source/version-scoped modules are removed on failure, preserving preexisting modules.
These checks establish local selected-source membership; installed/versioned selectors still do not prove identical full content or transitive dependencies across controller and worker environments.
Ordinary programmatic Direct tools and existing origin variants remain supported.
