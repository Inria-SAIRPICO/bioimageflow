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

Current processing transport uses `ProcessingTask`, `RowInvocation`, `ConsumedRow`, `OutputGroup`, and `ProcessingTaskResult` with explicit task/result v4 envelopes.
Each task carries the captured semantic Inputs/Outputs declaration: ordered fields, portable types, requiredness, nullability, image constraints and supported bounds.
The loaded worker instance must match that declaration before the scientific method runs; a successful result carries its actual declaration digest, checked before accepting outputs.
The shared declaration projector excludes class/module names, GUI hints and default values; effective defaults and resolved templates remain task argument values.
This attests the represented IO declaration without proving arbitrary initializer, custom validator or installed/transitive dependency behavior.
Physical task `mode` is separate from required `row_consumption`: mapped groups consume exactly one ordered input each and can emit zero, one or many outputs; a collective batch emits one group consuming the complete ordered input, including an empty input.
Collective `process_batch` returns a flat list of Outputs, without repeating an aggregate for each consumed row.
`ExecutionContext.batch_arguments` exposes admitted constants/defaults and resolved output paths as `Arguments`; `reference_rows` holds actual auxiliary `ReferenceRow(position, row_index, arguments)` records separately from consumed observation rows.
These values use the task's existing scoped reference grants; decoding and context construction do not open arrays.
Filesystem context serialization remains separate from batch values.
A SharedArray in any task value requires explicit scope admission; a missing scope is refused before importing tool code, while ordinary scalar tasks need no shared scope.
The public task/result codecs use the same typed grammar for arguments and outputs: primitive leaves, explicit dictionary/list/tuple nodes, Paths, SharedArray references, NumPy arrays, and dtype-preserving numeric NumPy scalars.
Literal dictionaries cannot collide with typed descriptors, and SharedArray decoding never allocates or attaches memory.
Unsupported objects, cyclic containers, object-containing dtypes, and malformed descriptors are refused; picklability alone is not sufficient.
SharedArray references are host-local, so distributed callers such as Parsl must refuse them even though local Wetlands workers can transport the reference.

### Shared-array ownership

SharedArray uses scoped numeric NPY2 file-backed mmap storage.
Producer allocation copies data once; controller `publish()`/`publish_value()` creates a separate accepted snapshot once per admission, so a retained producer writer cannot mutate accepted bytes.
Accepted opens use read-only zero-copy views, explicit writable opens refuse, and already sealed pass-through values reuse the same physical backing and bound owner.
Allocate under an explicit `SharedMemoryContext.activate()`; helper/context exit only unbinds.
References carry a strong local owner excluded from equality and wire; pure typed decoding is attachment-free, and workers borrow explicit task/input descriptors.
Controller close/release refuses new access while mapped views, exact group leases and admitted workers retain backing; `CleanupStatus` reports pending readers/grants/leases/files/errors until physical drain.
Workflow/engine completion never auto-closes returned references; no resource tracker or private unregister is involved.
Accepted and borrowed scientific inputs are immutable to consumers by contract; mutable processing creates a separate work/output value.

`retain(reference)` returns a metadata-only `SharedArrayLease`; `lease.project(group)` creates a local descriptor pin and `lease.release()` requests exact allocation cleanup after the last lease, reader and physical grant drains.
Local owner, lease and group pins are excluded from equality and typed wire values.
`content_identity(reference)` reads the accepted snapshot digest recorded during sealing; a mutable producer identity is only a current-byte planning preview, so authoritative cache execution publishes before computing its key.
SDK image writes refuse read-only backing through canonical paths and existing hardlink/symlink aliases; this is an SDK access policy, not a hostile Python or filesystem sandbox.
Normal reusable worker pools can retain physical grants; idle or quota-pressure retirement must drain the owning pool before reclaiming backing, while live mapped views remain valid.
If safe retirement cannot reclaim enough space, finite allocation budgets must refuse clearly.
BioImageFlow exposes exact returned group handles through `result_groups(result)`; ordinary result discard releases only its group leases.
The current owner-wide grant counter can conservatively delay cleanup behind another admitted task; group release does not force pool retirement or invalidate live views.
See the [library specifications](../../docs/source/specs.md) for the declaration, typed-value and resource-owner contracts.


Worker tool source admission
----------------------------

A standalone source-file origin hashes and executes one captured byte sequence, without reopening the source through importlib or using cached bytecode.
Worker loading checks the selected module and the tool class's defining module against the selected source file, package root or installed distribution members.
A cached version-scoped package from a different store root is refused rather than substituted or evicted.
Failed initialization or construction does not publish a tool instance; only newly admitted source/version-scoped modules are removed on failure, preserving preexisting modules.
These checks establish local selected-source membership; installed/versioned selectors still do not prove identical full content or transitive dependencies across controller and worker environments.
Ordinary programmatic Direct tools and existing origin variants remain supported.
Versioned local loading uses `bioimageflow_core.import_context.admit_import_root` and `selected_import_root` with `selected_installation` dependency authority.
A loaded dependency must have actual installed RECORD or supported editable-project membership and satisfy active declarations.
When the dependency is present in the selected installation, its exact selected distribution version must match; host-only providers use compatible metadata-owned versions.
Unknown or mismatched ownership refuses without replacing caller modules.
Managed workers explicitly use `managed_runtime`: the selected tool remains under its exact synthetic package path, while active dependencies come from the worker runtime and satisfy declared requirements without requiring controller/worker NumPy equality.
Managed loading never adds the entire controller tool-store root to the worker search path.
The shared context covers initialization, construction and scientific callbacks, restores caller search paths, and validates newly imported dependencies before successful return.
A primary tool error remains primary; post-return checks do not promise that arbitrary lazy imports were refused before method execution or initializer side effects.
Declared dependency versions and observed executor authority are distinct from dependency-byte equality and complete resolved recipe-generation proof.
