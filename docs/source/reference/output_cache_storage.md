# Output and Cache Storage Specification

This document defines the target v1 on-disk layout for BioImageFlow workflow outputs and cache storage.
It is a clean v1 target contract and does not preserve the legacy timestamp plus hash directory layout.
`docs/source/specs.md` summarizes the same public contract.

The canonical cache is the source of truth.
The human-facing output tree is a derived view over the canonical cache.

## Design Goals

- A partially written node result must never be reused as a cache hit.
- Concurrent workers may compute the same logical result without writing into the same mutable directory.
- Cache identity must not depend on runtime-only details such as hostnames, process IDs, temporary paths, scheduler job IDs, or shared-memory segment names.
- Cache records are immutable once visible to readers.
- Human-friendly paths may be mutable, but cache lookup must never depend on them.
- The v1 policy surface must stay small: one current-record policy, no background garbage collector, no automatic deletion of records, and no rich retention system.
- The layout must be versioned so incompatible future formats can coexist.

## Top-Level Layout

All paths below are rooted at `Workflow.storage_path`.

```text
<storage_path>/
  cache/
    v1/
      results/
        <result-shard>/
          <result-key>/
            attempts/
              <attempt-id>/
                attempt.json
                staging/
                  dataframe.parquet
                  dataframe.csv
                  assets/
                  work/
                failed.json
            records/
              <record-id>/
                manifest.json
                dataframe.parquet
                dataframe.csv
                assets/
            current.json
            conflicts/
              <conflict-id>.json
  runs/
    <run-id>/
      run.json
      nodes/
        <node-key>/
          result.json
          record.bioimageflow-link.json
          outputs/
    latest-success.bioimageflow-link.json
  latest/
    <node-key>.bioimageflow-link.json
  provenance_graph.json
```

`cache/v1/` is private machine-readable storage.
`runs/` and `latest/` are user-facing views.
`provenance_graph.json` remains a workflow-level provenance artifact and is outside the cache namespace.

## Terms and Identifiers

### Node Key

`<node-key>` is a storage-safe identifier for one node instance in one workflow.
It is used in user-facing paths and run manifests.

The node key is not the cache key.
Renaming a display label may change the human-facing node key without changing cache identity, depending on workflow identity rules.

Node keys must be normalized before use as path segments:

- Use UTF-8 text normalized to NFC.
- Forbid empty segments, `.` and `..`.
- Forbid path separators, NUL bytes, and platform-reserved names.
- Avoid case-only distinctions because common filesystems may be case-insensitive.
- Bound the encoded segment length.
- Append a short stable disambiguator when two display names normalize to the same storage segment.

### Result Key

`<result-key>` is the final cache lookup key.
It answers: "what computation is this, over which exact selected upstream records?"

The result key must include every value that can affect the logical result and cache validity, including:

- BioImageFlow cache schema version.
- Workflow or workflow-fragment identity when relevant.
- Node definition identity.
- Tool identity and version.
- Environment dependency hash.
- Normalized parameters and statically declared input bindings or selectors.
- Selected upstream record references for every cacheable upstream value consumed by this node.
- Declared external references consumed by this node.
- Development-mode source hash when development mode is enabled.
- Output contract version when output schema changes affect cache compatibility.

An upstream record reference contains the upstream node key, upstream result key, upstream record ID, and the statically declared binding or selector through which the value is consumed.
V1 uses the whole upstream record ID.
It does not attempt per-row or per-column content hashing.

The result key must not include:

- Run ID.
- Attempt ID.
- Record ID for the node currently being computed.
- Wall-clock timestamps.
- Hostnames.
- Process IDs.
- Scheduler job IDs.
- Absolute attempt paths.
- Shared-memory segment names.
- Human-facing output symlink paths.

If any consumed upstream value has no selected immutable record, the downstream node cannot produce a reusable result key in v1.
The downstream execution may still run, but cache lookup and reusable cache write are disabled for that path.

External references are durable values outside the record directory, such as source image paths discovered by a file-listing source node.
V1 external reference identity is path-based to preserve the current BioImageFlow cache semantics.
The identity material is the normalized absolute path string plus the declared reference kind.
Content digests, size, and mtime-based invalidation are deferred to a later explicit input-fingerprinting feature.

The result key should be encoded as a namespaced digest token, for example:

```text
rk_<base32-sha256>
```

The exact digest encoding is part of the v1 storage contract.
The digest length must be collision-resistant enough that cache corruption by digest collision is not a practical concern.

### Result Shard

`<result-shard>` is a short prefix derived from `<result-key>` to prevent very large single directories.
For example, the first four digest characters may be split as:

```text
cache/v1/results/ab/cd/rk_abcdef...
```

If the implementation uses nested shard directories, `<result-shard>` means the full shard path.
Sharding is deterministic from `<result-key>`.

### Attempt ID

`<attempt-id>` identifies one concrete execution attempt by one worker for one result key.
It answers: "which worker tried to compute this?"

Attempt IDs are not deterministic and are not part of cache identity.
They exist so concurrent workers do not write into the same mutable directory.

Attempt IDs must be globally unique enough for distributed execution and should be lexicographically time-sortable.
A ULID-style identifier is appropriate.

Example:

```text
01K7M9Y2EHJ6V9X3G7K8Q4T2BR
```

Two workers computing the same `<result-key>` must create different attempt directories.
Multiple workers writing into the same attempt directory is a bug unless a future engine explicitly implements a single-writer claim protocol.

### Record ID

`<record-id>` identifies immutable content produced by a successful attempt.
It answers: "what result content was produced?"

A record ID is derived from a canonical content manifest, not from execution metadata.
It must be stable for equivalent published content.

The record ID is a content identity scoped by its manifest.
Its filesystem location is still under the result key:

```text
cache/v1/results/<result-shard>/<result-key>/records/<record-id>/
```

The content manifest used for the record hash includes:

- The result key.
- The canonical dataframe schema and canonical dataframe artifact digest.
- Declared output asset paths, sizes, and digests.
- Content-affecting metadata required to interpret the outputs.

The content manifest used for the record hash excludes:

- Record ID itself.
- Attempt ID.
- Run ID.
- Creation or publication timestamps.
- Hostnames.
- Process IDs.
- Scheduler job IDs.
- Runtime duration.
- Logs unless logs are declared output artifacts.

This split prevents the record ID from changing only because the same content was produced by a different worker or at a different time.

### Run ID

`<run-id>` identifies one workflow execution initiated by a user, GUI, API call, or scheduler.
It answers: "which workflow run used these node results?"

A run may use a mix of newly computed records and cache hits from previous runs.
One run ID can therefore reference many record IDs.

Run IDs should be unique and lexicographically time-sortable.
They are for provenance and human navigation, not cache identity.

## Directory Semantics

### `attempts/`

`attempts/` contains mutable worker-owned execution attempts.
Attempt directories are never cache hits.

An attempt directory may be incomplete, internally inconsistent, or changing while the worker runs.
It may contain temporary files, partially written dataframe files, partially written assets, row scratch files, batch scratch files, and logs.

No reader may use files from `attempts/` as reusable cache output.
Downstream nodes, cache lookup, `runs/`, and `latest/` must point only to records selected by `current.json`.

### `records/`

`records/` contains immutable reusable cache records.
A record has passed the publication protocol and may be reused by cache lookup.

After publication, the engine must not modify files inside `records/<record-id>/`.
If a new execution produces different content for the same result key, it creates a different record directory.

V1 automatic garbage collection must not delete record directories.
Published-record pruning is a future explicit user operation.

### `current.json`

`current.json` points to the selected reusable record for one result key.
V1 has one selection policy: `first-valid`.

`current.json` is not the source of truth for record contents.
The source of truth is `records/<record-id>/manifest.json` plus the files declared by that manifest.

If `current.json` is missing, cache lookup treats the result key as a miss.
If valid record directories exist but `current.json` is missing after a crash, normal lookup must not pick a winner by filesystem iteration or mtime.
An explicit repair operation may rebuild `current.json`, but it must report ambiguity when multiple valid records exist.

If `current.json` is corrupt, points outside the result-key directory, or points to a missing or invalid record, lookup must raise a cache corruption error.
Normal publication must not silently replace an invalid current pointer; repair is a separate operation.

Required fields:

```json
{
  "schema": "bioimageflow.cache.current.v1",
  "result_key": "rk_...",
  "record_id": "rec_...",
  "manifest": "records/rec_.../manifest.json",
  "policy": "first-valid",
  "selected_at": "2026-06-16T12:00:00Z",
  "selected_by": {
    "attempt_id": "01K7M9Y2EHJ6V9X3G7K8Q4T2BR",
    "run_id": "01K7M9Y2ABCD..."
  }
}
```

### `conflicts/`

`conflicts/` contains diagnostic reports for cases where the same result key produced a different valid record than the current record.

The correctness rule is that the existing current record remains selected.
Conflict reports are diagnostics and must not be required for cache lookup correctness.

For deterministic nodes, a conflict means at least one of these is true:

- The result key omitted a value that affects output.
- The tool is nondeterministic despite being treated as deterministic.
- The runtime environment changed without changing the environment hash.
- The storage state was corrupted.

For explicitly cacheable nondeterministic nodes, alternate records may be expected.
V1 still keeps the first valid record current and records alternates diagnostically.

## Record Validity

A record is valid only if all of the following hold:

- The record directory is under `records/<record-id>/`.
- `manifest.json` exists and has schema `bioimageflow.cache.record.v1`.
- `manifest.json` names the enclosing result key and record ID.
- `dataframe.parquet` exists and is the canonical dataframe.
- Every declared asset listed in the manifest exists.
- Manifest paths are normalized relative POSIX paths.
- Manifest paths are not absolute and do not contain `..`.
- Manifest paths do not escape the record directory through symlinks.
- Required sizes and digests in the manifest match the files on disk.
- The record ID recomputed from the canonical content manifest matches the directory name.

Anything else is ignored for cache lookup or reported as corruption.

## Dataframe Contract

`dataframe.parquet` is mandatory and canonical for every reusable record.
`dataframe.csv` is optional human/debug output and is never authoritative.

The record ID must not hash incidental Parquet writer metadata.
Instead, it hashes a canonical dataframe digest plus declared asset digests.

V1 canonical dataframe digest rules:

- The row sequence is the engine-produced dataframe order after execution and path canonicalization.
- The index is included as strings in that row sequence.
- Columns are serialized in deterministic order: declared output schema order first, then additional columns sorted lexicographically by name.
- Supported scalar cell kinds are null, bool, signed integer, unsigned integer, float, string, record asset reference, and external path reference.
- Integers are serialized as decimal strings with their signedness recorded in the column schema.
- Floats are serialized using a deterministic representation; NaN, positive infinity, and negative infinity use explicit sentinel strings.
- Strings are UTF-8 NFC-normalized.
- Datetime values are serialized as UTC ISO-8601 strings with timezone policy recorded in the column schema.
- Categorical values are serialized as their string labels with category metadata recorded when available.
- Unsupported object values must be converted by the tool or rejected before publication.
- The canonical dataframe payload is encoded as canonical JSON with sorted object keys and compact separators, then hashed with SHA-256.

The Parquet file must contain the same logical values as the canonical dataframe digest.
The implementation must choose and document a deterministic Parquet writer configuration before enabling distributed execution across heterogeneous workers.

## Path and Asset Canonicalization

Path canonicalization applies only to columns declared as path or asset columns by the tool output schema or engine-resolved output schema.
The engine must not infer asset columns heuristically from arbitrary strings.

Before computing the record ID and writing the published dataframe, the engine canonicalizes declared path/asset columns:

- A declared `ProcessingTool.Outputs` path field whose value is under the attempt `staging/assets/` tree is an owned asset and is rewritten to a record-relative artifact reference such as `assets/mask.tif`.
- A path under the attempt `staging/work/` tree is rejected unless the tool explicitly declared that file as an output artifact.
- Two outputs resolving to the same canonical `assets/...` path are an error unless the tool returns the same file and the manifest records it once.
- Absolute paths, `..`, symlink escapes, and platform-specific aliases must not appear in record-owned path columns.
- Source-tool and `DataFrameTool` path columns are external references unless explicitly declared as owned assets.
- A legitimate external path, such as a source file path emitted by a source node, is represented as a declared external reference and included in result-key hash material using the v1 path-based external identity.

Manifest entries distinguish owned assets from external references:

```json
{
  "outputs": [
    {"path": "assets/mask.tif", "kind": "owned_asset", "size": 123456, "digest": "sha256:..."},
    {"path": "/data/raw/cell_001.tif", "kind": "external_path", "identity": "path"}
  ]
}
```

When the engine loads a cache hit for runtime execution, it resolves record-relative asset paths to absolute paths under the selected record directory before passing values to downstream tools.
It must reject paths that resolve outside the record directory.

This preserves the runtime contract that tools receive usable absolute paths while preventing record IDs and cache hits from depending on attempt-local directories.

## Attempt Lifecycle

An attempt starts as a private workspace:

```text
cache/v1/results/<result-shard>/<result-key>/attempts/<attempt-id>/
  attempt.json
  staging/
```

`attempt.json` records at least:

- Schema name and version.
- Result key.
- Attempt ID.
- Run ID that created the attempt.
- Node key.
- Tool identity.
- Start timestamp.
- Runtime engine.
- Worker identity for diagnostics only.

The worker writes generated files under `staging/`.
For `ProcessingTool` nodes, the execution context points into the attempt staging tree.

`assets/` contains declared output artifacts.
`work/` contains scratch and intermediate files.
Files in `work/` are not part of the public output contract unless the tool explicitly returns them and the manifest declares them as assets.

The engine may write `failed.json` after execution failure.
`failed.json` is diagnostic only and must not make the attempt reusable.

## Publication Protocol

Publication is the transition from mutable attempt state to an immutable record.
The protocol must make partially written output invisible to cache lookup.

The high-level sequence is:

1. Create a unique attempt directory.
2. Write all outputs into the attempt `staging/` tree.
3. Flush and close all writer handles.
4. Canonicalize dataframe path columns.
5. Build a canonical content manifest from staged files.
6. Compute `<record-id>` from the canonical content manifest.
7. Create a temporary record directory under the same result-key directory.
8. Move or copy finalized staged content into the temporary record directory.
9. Write `manifest.json` in the temporary record directory.
10. Atomically install the temporary directory as `records/<record-id>/`, or detect that an equivalent record already exists.
11. Under a per-result-key guarded metadata update, create `current.json` if absent.
12. Bind the run to the selected current record.
13. Update run and latest views from the selected current record.

All atomic renames must happen within the same filesystem.
Where supported, files and parent directories should be fsynced before the final rename that makes a record visible.

The guarded metadata update covers reading current state, deciding the outcome, and writing `current.json` or a conflict report.
It must not be held while executing tools or copying large output payloads.

Acceptable guard implementations include atomic create-if-absent, an atomic directory lock, a backend compare-and-swap primitive, or documented filesystem locking known to be safe for the configured filesystem.
Atomic rename of `current.json` prevents torn writes but is not sufficient by itself.

Publication outcomes:

- If `current.json` is absent, the candidate record becomes current.
- If `current.json` already points to the same record ID, the attempt is a duplicate success.
- If `current.json` points to a different valid record ID, the existing current record remains selected and the candidate is a conflict or alternate.
- If `current.json` points to an invalid or missing record, publication reports corruption and does not silently replace it.

The selected record binding is mandatory.
If an attempt produces `rec_A` but `current.json` already selects `rec_B`, the current run must use `rec_B` downstream or stop/report the conflict.
It must not silently feed non-current `rec_A` into downstream nodes.

## Workflow Planning Semantics

`Workflow.plan()` reports final result keys only when every consumed upstream selected record ID is known.
When it reports a final result key, `compute()` must derive the same result key if it consumes the same upstream record references.

If any upstream node would need execution before its selected record is known, the downstream final result key is unknown.
The plan entry should report a pending-upstream status rather than a fake final key.

A diagnostic logical signature may exist for debugging, but it is not a cache lookup key and must not be presented as byte-identical to compute.
The public API must not reuse one field, such as `sig_hash`, for both final result keys and diagnostic signatures.

Recommended plan entry fields:

```text
final_result_key: str | None
selected_record_id: str | None
status: CACHED | OUT_OF_DATE | UNEXECUTED | SKIPPED | PENDING_UPSTREAM
pending_upstreams: tuple[str, ...]
```

`plan()` is a storage snapshot.
If `current.json` changes between `plan()` and `compute()`, compute may legitimately consume a different upstream record unless execution is explicitly bound to the plan's selected record IDs.

## Conflict Reports

A conflict report is written under:

```text
cache/v1/results/<result-shard>/<result-key>/conflicts/<conflict-id>.json
```

It records:

- Schema name and version.
- Result key.
- Current record ID.
- Candidate record ID.
- Attempt ID that produced the candidate.
- Run ID that produced the candidate.
- Determinism declaration.
- Summary of manifest differences when practical.
- Timestamp.

Conflict reports are best-effort diagnostics.
Failure to write a conflict report must not make a non-current candidate record selected.

## Human-Facing Run Views

Each workflow execution creates a run directory:

```text
runs/<run-id>/
  run.json
  nodes/
    <node-key>/
      result.json
      record.bioimageflow-link.json
      outputs/
```

`run.json` records workflow-level provenance:

- Schema name and version.
- Run ID.
- Workflow identity.
- Storage path.
- Start and completion timestamps.
- Engine.
- BioImageFlow version.
- Requested target nodes.
- Overall status.

`nodes/<node-key>/result.json` records the exact selected record used by that node in that run:

```json
{
  "schema": "bioimageflow.run.node_result.v1",
  "run_id": "01K7M9Y2ABCD...",
  "node_key": "segmentation",
  "result_key": "rk_...",
  "record_id": "rec_...",
  "cache_hit": true,
  "canonical": "../../../../cache/v1/results/ab/cd/rk_.../records/rec_..."
}
```

`record.bioimageflow-link.json` is a pointer file to the selected canonical record.
`outputs/` may contain pointer files to individual user-facing artifacts for convenient browsing.

Example:

```text
runs/<run-id>/nodes/segmentation/record.bioimageflow-link.json
runs/<run-id>/nodes/segmentation/outputs/mask.tif.bioimageflow-link.json
```

The run view records what the run used, including cache hits from previous runs.
It must not be used to decide cache hits.

## Latest View

`latest/` is a mutable per-node convenience view.
Each entry points to the latest successful result for that node, even if the workflow run later fails on another node.

```text
latest/<node-key>.bioimageflow-link.json
```

The latest fully successful workflow run is tracked separately with a pointer file:

```text
runs/latest-success.bioimageflow-link.json
```

The latest view should be updated after the run view has been written.
Updating `latest/<node-key>` must be atomic:

1. Create a temporary pointer file in the same parent directory.
2. Atomically replace the old latest entry with the temporary entry.

If a workflow run fails or is cancelled, the engine may still update `latest/<node-key>` for nodes that successfully resolved to a selected record before the failure.
It must not update `runs/latest-success.bioimageflow-link.json` unless the whole requested workflow run completed successfully.

The latest view is allowed to change over time.
It must not be included in result-key hashing.

## Pointer Files and Symlink Export

The engine writes pointer files by default for run and latest views.
This avoids platform-specific symlink permissions and keeps the public run view portable.

Directory pointer example:

```text
runs/<run-id>/nodes/<node-key>/record.bioimageflow-link.json
```

```json
{
  "schema": "bioimageflow.link.v1",
  "kind": "directory",
  "target": "../../../../cache/v1/results/ab/cd/rk_.../records/rec_..."
}
```

File pointer example:

```text
runs/<run-id>/nodes/<node-key>/outputs/mask.tif.bioimageflow-link.json
```

```json
{
  "schema": "bioimageflow.link.v1",
  "kind": "file",
  "target": "../record/assets/mask.tif",
  "digest": "sha256:..."
}
```

Pointer files must be written by temp-file plus atomic rename.
Pointer targets should use normalized relative paths when possible.

The engine may offer export modes that materialize real symlinks or copied files for users.
Exported symlinks and copied files are not canonical cache records.

## Invalidation, Retention, and Transient Cleanup

V1 invalidation changes cache selection state; it does not delete record directories automatically.
Invalidating a node removes or tombstones `current.json` for affected result keys and downstream result keys according to workflow dependency rules.
Invalidation must use the same per-result-key guarded metadata update discipline as publication, or coordinate externally with active compute operations for the same workflow storage path.

The legacy `max_executions` and `max_age` Workflow settings are removed from the clean v1 public API and must not delete `records/<record-id>/`.
If legacy configuration is encountered during migration, it must be ignored for v1 cache records or handled by an explicit migration layer outside the clean v1 API.

V1 core does not define a background garbage collector.
An explicit `cleanup_transients()` operation may remove transient, non-canonical state:

- Stale incomplete attempts.
- Failed attempts.
- Stale temporary record directories.

`cleanup_transients()` must not delete `records/<record-id>/` directories.
Published-record pruning is a future explicit user operation and requires its own retention policy, dry-run behavior, and active-reader protection.

Transient cleanup may remove an attempt or temporary record directory only when both are true:

- The target is older than the configured stale threshold.
- The caller has established that no compute or publish operation can still be using it, either by external coordination or by a future lease mechanism.

Leases, pins, reader protection, and automatic record pruning are deferred from v1.

## Shared Memory Interaction

Shared memory is a runtime transport optimization, not a canonical storage format.

Shared-memory segment names must not appear in result keys or record IDs.
Records must contain durable file artifacts or manifests that can be read by a different process or machine.

If a tool produces a `SharedArray` during execution and the result is cacheable, the engine must publish a durable representation before a cache hit can be served to a later run.
The durable representation belongs in the selected record and is described by the manifest.

V1 represents reusable shared-memory outputs as record-owned assets.
For example, a shared-memory dataframe cell may be canonicalized to:

```text
assets/shm/<column>/<row-key>.npy
```

The manifest records the array dtype, shape, order, and digest.
When the engine loads the cache hit for runtime execution, it may recreate a fresh `SharedArray` with a new node-local shared-memory name from the durable asset.
The shared-memory name created during reload is runtime state and is not part of the result key or record ID.

This rule does not require every shared-memory value to be eagerly copied to disk during active in-memory pipelines.
It only requires that a reusable cache record not depend on a node-local shared-memory handle.

## Repair

A repair command may:

- Rebuild `current.json` from valid records when there is exactly one unambiguous valid record.
- Report ambiguity when multiple valid records exist without a current pointer.
- Validate manifests and artifact presence.
- Remove stale temporary record directories.
- Remove or report broken symlinks.
- Remove or report conflicts.

Repair must not silently choose a different current record when the existing state contains conflicts unless the user requested that policy.

## Compatibility With Legacy Storage

This document intentionally replaces the legacy storage shape documented in the exhaustive specification:

```text
data/<node_name>/<YYYYMMDD_HHMMSS>_<hash12>/
```

The replacement separates logical computation identity, execution attempts, immutable records, and human-facing views:

```text
cache/v1/results/<result-shard>/<result-key>/
  attempts/<attempt-id>/
  records/<record-id>/
  current.json

runs/<run-id>/
latest/
```

`docs/source/specs.md` summarizes this contract and should remain aligned with this reference document.
