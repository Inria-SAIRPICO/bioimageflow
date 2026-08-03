# Output and Cache Storage Specification

This document defines the on-disk layout for BioImageFlow workflow outputs and cache storage.
`docs/source/specs.md` summarizes the same public contract.

The canonical cache is the source of truth.
The human-facing output tree is a derived view over the canonical cache.

## Design Goals

- A partially written node result must never be reused as a cache hit.
- Concurrent workers may compute the same logical result without writing into the same mutable directory.
- Cache identity must not depend on runtime-only details such as hostnames, process IDs, temporary paths, scheduler job IDs, or shared-memory segment names.
- Cache records are immutable once visible to readers.
- Human-friendly paths may be mutable, but cache lookup must never depend on them.
- The storage policy surface must stay small: one current-record policy, no background garbage collector, no automatic deletion of records, and no rich retention system.
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
                  dataframe.csv           # optional debug artifact
                  assets/
                  work/
            records/
              <record-id>/
                manifest.json
                dataframe.parquet
                dataframe.csv             # optional debug artifact
                assets/
            current.json
            conflicts/
              <conflict-id>.json
      transient/
        runs/
          <run-id>/
            nodes/
              <node-key>/
                <invocation-id>/
                  invocation.json
                  assets/
                  work/
                  failed.json            # optional failure metadata
  diagnostics/
    v1/
      runs/
        <run-id>/
          nodes/
            <node-key>/
              <invocation-id>/
                tasks/
                  <task-id>.json
  launcher/
    v1/
      runs/
        <run-id>/
          submission.json
          status.json
          progress.jsonl
          execution.claim
          claims/
          cancel_requested
          error.json
          command.json
          logs/
            orchestrator.out
            orchestrator.err
          inputs/
          return/
            manifest.json
            dataframes/
            assets/
  views/
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
  outputs/
    runs/
    latest/
```

`cache/v1/` is private machine-readable storage.
`diagnostics/v1/` contains mutable backend-task execution metadata that is separate from reusable result identity.
`launcher/v1/` contains submitted-run control state and durable public return snapshots.
`views/` contains portable JSON provenance and pointer views.
`outputs/` contains optional materialized files for human browsing.

## Terms and Identifiers

### Node Key

`<node-key>` is a storage-safe identifier for one node instance in one workflow.
It is used in user-facing paths and run manifests.

The node key is not the cache key.
Renaming a display label may change the human-facing node key without changing cache identity, depending on workflow identity rules.

BioImageFlow normalizes node keys before use as path segments:

- Use UTF-8 text normalized to NFC.
- Forbid empty segments, `.` and `..`.
- Forbid path separators, NUL bytes, and platform-reserved names.
- Avoid case-only distinctions because common filesystems may be case-insensitive.
- Bound the encoded segment length.
- Append a short stable disambiguator when two display names normalize to the same storage segment.

Runtime run views use validated scoped node names directly.
Workflow node names must be valid storage path segments before they can appear in user-facing run-view paths.

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
The current layout uses the whole upstream record ID.
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

If any consumed upstream value has no selected immutable record, the downstream node cannot produce a reusable result key for reusable caching.
The downstream execution may still run, but cache lookup and reusable cache write are disabled for that path.

External references are durable values outside the record directory, such as source image paths discovered by a file-listing source node.
External reference identity is path-based.
The identity material is the normalized absolute path string plus the declared reference kind.
Input content fingerprinting is outside the current path-based external-reference contract.

The result key should be encoded as a namespaced digest token, for example:

```text
rk_<base32-sha256>
```

The exact digest encoding is part of the storage contract.
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

Attempt IDs use `att_` followed by 32 lowercase hexadecimal characters generated from a UUID4 value.
The grammar is `^att_[0-9a-f]{32}$`.

Example:

```text
att_a791366f6a8e4fc6a7428bf8e69b57c1
```

Two workers computing the same `<result-key>` must create different attempt directories.
Engines must either publish from distinct attempt directories or implement a single-writer claim protocol.

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
- The canonical dataframe logical schema and logical digest.
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
- The dataframe transport digest.
- Logs unless logs are declared output artifacts.

This split prevents the record ID from changing only because the same content was produced by a different worker or at a different time.

### Run ID

`<run-id>` identifies one workflow execution initiated by a user, GUI, API call, or scheduler.
It answers: "which workflow run used these node results?"

A run may use a mix of newly computed records and cache hits from previous runs.
One run ID can therefore reference many record IDs.

Run IDs use `run_` followed by 32 lowercase hexadecimal characters generated from a UUID4 value.
The grammar is `^run_[0-9a-f]{32}$`.
They are for provenance and human navigation, not cache identity.

### Invocation ID

`<invocation-id>` identifies one concrete execution of a `ProcessingTool` node within a workflow run.
It is required whether the node has a reusable result key or uses transient storage.

Invocation IDs are non-content correlation values.
They use `inv_` followed by 32 lowercase hexadecimal characters generated from a UUID4 value.
The grammar is `^inv_[0-9a-f]{32}$`.
They are unique within the run, are safe single path segments, and never enter result-key or record-ID material.
For reusable execution, the invocation ID and cache attempt ID remain distinct fields even when one implementation creates them together.
For non-reusable execution, no cache attempt ID exists.

## Directory Semantics

### `attempts/`

`attempts/` contains mutable worker-owned execution attempts.
Attempt directories are never cache hits.

An attempt directory may be incomplete, internally inconsistent, or changing while the worker runs.
It may contain temporary files, partially written dataframe files, partially written assets, row scratch files, batch scratch files, and logs.

No reader may use files from `attempts/` as reusable cache output.
Downstream nodes, cache lookup, `views/runs/`, and `views/latest/` must point only to records selected by `current.json`.

### `records/`

`records/` contains immutable reusable cache records.
A record has passed the publication protocol and may be reused by cache lookup.

After publication, the engine must not modify files inside `records/<record-id>/`.
If a new execution produces different content for the same result key, it creates a different record directory.

Automatic garbage collection must not delete record directories.
Published-record pruning is an explicit user operation.

### `current.json`

`current.json` points to the selected reusable record for one result key.
The current layout has one selection policy: `first-valid`.

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
    "attempt_id": "att_a791366f6a8e4fc6a7428bf8e69b57c1",
    "run_id": "run_8e42fd04e27749e2b86791239b6368df"
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

### `transient/runs/`

`cache/v1/transient/runs/<run-id>/nodes/<node-key>/<invocation-id>/` is the only workspace for a non-reusable `ProcessingTool` invocation.
The node key is the validated scoped node path, represented as individually validated path segments.
The run ID and invocation ID are validated storage-safe segments.

The directory has these semantics:

- `assets/` contains engine-owned declared outputs and is the only location beneath the invocation that may be returned as an owned runtime path.
- `work/` contains scratch files and is never a declared output.
- `invocation.json` contains non-content diagnostics: schema, run ID, scoped node key, invocation ID, effective engine, start time, terminal status, and terminal time.
- `failed.json` may contain normalized failure diagnostics.
- Every path is constructed through storage helpers, resolved beneath the invocation root, and rejected on traversal, symlink escape, unsafe segment, or case-normalization collision.
- No result-key directory, cache attempt, immutable record, `current.json`, conflict report, run-node record pointer, latest pointer, or output projection is created for the invocation.
- Progress events for the invocation carry neither a result key nor a record ID.

Successful transient assets remain available after the attached call returns.
They are retained until an explicit transient cleanup operation removes them; execution completion does not delete them automatically.
Callers that need a longer retention guarantee must copy or export those assets before requesting cleanup.

Transient cleanup may remove a terminal invocation only after the configured stale threshold and only when the caller has established that no engine, task, or other process can still write to or consume it.
An engine keeps the invocation active until every submitted writer is terminal.
Cross-process cleanup requires external coordination or a lease mechanism; age alone is never sufficient.

## Record Validity

A record is valid only if all of the following hold:

- The record directory is under `records/<record-id>/`.
- `manifest.json` exists and has schema `bioimageflow.cache.record.v1`.
- `manifest.json` names the enclosing result key and record ID.
- `dataframe.parquet` exists and is the canonical dataframe.
- The Parquet byte digest matches `dataframe.transport_digest`.
- The dataframe's recomputed canonical logical schema and digest match `dataframe.logical_schema` and `dataframe.logical_digest`.
- Every declared asset listed in the manifest exists.
- Every owned asset declares `asset_type`, and its file or complete directory-tree size and digest match.
- Manifest paths are normalized relative POSIX paths.
- Manifest paths are not absolute and do not contain `..`.
- Manifest paths do not escape the record directory through symlinks.
- Required sizes and digests in the manifest match the files on disk.
- The record ID recomputed from the canonical content manifest matches the directory name.

Anything else is ignored for cache lookup or reported as corruption.

## Dataframe Contract

`dataframe.parquet` is mandatory for every reusable record.
`dataframe.csv` is optional human/debug output and is never authoritative.

The record manifest distinguishes logical identity from transport integrity:

```json
{
  "dataframe": {
    "path": "dataframe.parquet",
    "format": "parquet",
    "logical_digest": "sha256:...",
    "transport_digest": "sha256:...",
    "logical_schema": [
      {"name": "mask", "dtype": "object", "kind": "record_asset"}
    ]
  }
}
```

`logical_digest` and `logical_schema` enter record identity.
`transport_digest` is the SHA-256 digest of the stored Parquet bytes and is validated when the artifact is read, but it does not enter record identity.
Record validation loads the Parquet artifact, reconstructs the canonical logical payload using `logical_schema`, and requires the recomputed logical digest to match.

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
The canonical writer converts the prepared dataframe with `pyarrow.Table.from_pandas(..., preserve_index=True)` and writes it with:

- Parquet format version `2.6`,
- data-page version `1.0`,
- Zstandard compression at level `3`,
- dictionary encoding disabled,
- statistics and page indexes disabled,
- timestamp coercion to microseconds with truncation rejected,
- Arrow schema storage enabled,
- compliant nested types enabled,
- a fixed row-group size of 65,536 rows.

All supported pandas and PyArrow versions must round-trip to the same logical payload under these settings.
Byte-for-byte Parquet equality across dependency versions is not required because transport bytes are integrity metadata rather than content identity.

Root DataFrame inputs use the same canonical logical payload and digest helper.
For a root DataFrame, Python `Path` cells are normalized absolute `external_path` values, string cells remain strings, supported scalar and datetime cells use the rules above, and unsupported object cells are rejected.
A root DataFrame does not contain a `record_asset` value unless the caller supplies an explicit immutable record reference through a separately documented binding contract.
Input transport paths and Parquet transport digests never enter root DataFrame identity.

## Path and Asset Canonicalization

Path canonicalization applies only to columns declared as path or asset columns by the tool output schema or engine-resolved output schema.
The engine must not infer asset columns heuristically from arbitrary strings.

Before computing the record ID and writing the published dataframe, the engine canonicalizes declared path/asset columns:

- A declared `ProcessingTool.Outputs` path field whose value is under the attempt `staging/assets/` tree is an owned asset and is rewritten to a record-relative artifact reference such as `assets/mask.tif`.
- If a declared templated path output is resolved and the tool writes the file but returns zero dataframe rows, the written file is still published as an owned asset in `manifest.outputs`. The dataframe remains empty; publication must not add sentinel rows solely to expose artifacts.
- If a table-only `ProcessingTool` declares `zero_row_scalar_outputs`, each input row that returns zero dataframe rows publishes the declared scalar values as `scalar_output` entries in `manifest.outputs`. These entries are provenance metadata only; they are not rehydrated into dataframe rows.
- A path under the attempt `staging/work/` tree is rejected unless the tool explicitly declared that file as an output artifact.
- Two outputs resolving to the same canonical `assets/...` path are an error unless the tool returns the same file and the manifest records it once.
- Absolute paths, `..`, symlink escapes, and platform-specific aliases must not appear in record-owned path columns.
- Source-tool and `DataFrameTool` path columns are external references unless explicitly declared as owned assets.
- A legitimate external path, such as a source file path emitted by a source node, is represented as a declared external reference and included in result-key hash material using the path-based external identity.

Manifest entries distinguish owned assets from external references:

```json
{
  "outputs": [
    {"path": "assets/mask.tif", "kind": "owned_asset", "asset_type": "file", "size": 123456, "digest": "sha256:..."},
    {"path": "/data/raw/cell_001.tif", "kind": "external_path", "identity": "path"}
  ]
}
```

Every `owned_asset` has an explicit `asset_type` of `file` or `directory`.

For a directory asset, the manifest entry describes the directory root and its complete recursive identity:

```json
{
  "kind": "owned_asset",
  "asset_type": "directory",
  "path": "assets/dataset.zarr",
  "size": 123456,
  "digest": "sha256:..."
}
```

The directory digest is SHA-256 over canonical JSON with schema `bioimageflow.directory_asset.v1` and a list sorted by normalized relative POSIX path.
A directory entry contains `{"type": "directory", "path": "..."}`.
A regular-file entry contains `{"type": "file", "path": "...", "size": <bytes>, "digest": "sha256:..."}`.
The root directory itself is not an entry, while empty descendant directories are entries and therefore affect identity.
The manifest `size` is the sum of regular-file sizes.

Directory scanning NFC-normalizes every relative path and rejects absolute paths, empty segments, `.`, `..`, NUL, backslashes, symlinks, sockets, devices, FIFOs, and other special files.
It also rejects duplicate normalized paths, file/directory prefix collisions, and paths that collide under Unicode case folding.
Record validation recomputes the tree identity using the same rules.

Scalar output metadata uses the same deterministic scalar payload encoding as canonical dataframe cells:

```json
{
  "outputs": [
    {
      "kind": "scalar_output",
      "output_column": "spot_count",
      "row_index": "0",
      "value": {"kind": "signed_integer", "value": "0"}
    }
  ]
}
```

`scalar_output` entries must reference declared scalar output columns and a string row index from the input execution row that produced no output rows.
They are included in record identity and run node `result.json`, but they do not create files below run-view `outputs/`.

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
- Optional invocation ID.
- Status: initially `running`, then exactly one of `succeeded`, `failed`, or `cancelled`.
- Terminal timestamp and error type.

The worker writes generated files under `staging/`.
For `ProcessingTool` nodes, the execution context points into the attempt staging tree.

`assets/` contains declared output artifacts.
`work/` contains scratch and intermediate files.
Files in `work/` are not part of the public output contract unless the tool explicitly returns them and the manifest declares them as assets.

The engine terminalizes `attempt.json` only after all possible writers for that attempt have stopped.
Attempt metadata never makes an attempt reusable.

## Backend Task Diagnostics

Every submitted backend task has a separate mutable diagnostic:

```text
diagnostics/v1/runs/<run-id>/nodes/<node-key>/<invocation-id>/tasks/<task-id>.json
```

The `bioimageflow.backend_task.v1` object records the backend, run, scoped node, invocation, optional cache attempt, task, executor label, task retry, dispatch mode, aligned row positions, canonical tool origin, status, submission time, completion time, and terminal error type.
The initial status is `submitted`.
The engine changes it exactly once to `succeeded`, `failed`, or `cancelled` after observing or draining the corresponding future.

Backend diagnostics are not record content, never enter result-key or record-ID material, and are not required for cache lookup.

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
7. Create a private `records/.candidate.<attempt-id>.<nonce>/<record-id>/` directory on the record filesystem.
8. Move or copy finalized staged content into that private candidate directory.
9. Write `manifest.json` and validate the complete candidate in place.
10. Atomically rename the complete candidate to `records/<record-id>/`, or validate and use an equivalent concurrently installed record.
11. Create a complete candidate current-pointer file and atomically hard-link it to `current.json` only if `current.json` is absent.
12. Bind the run to the selected current record.
13. Update run and latest views from the selected current record.

All atomic renames must happen within the same filesystem.
Where supported, files and parent directories should be fsynced before the final rename that makes a record visible.

The hard-link create-if-absent operation is the first-valid compare-and-set.
If another publisher wins, the loser validates and loads the selected record before it reports a conflict or continues downstream.
Tool execution and record construction happen before this selection operation.

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
Final result keys and diagnostic signatures are separate fields.

Recommended plan entry fields:

```text
final_result_key: str | None
selected_record_id: str | None
status: CACHED | PRIOR_SELECTION_MISS | UNEXECUTED | SKIPPED | PENDING_UPSTREAM
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

## Submitted-Run Launcher Namespace

`launcher/v1/` is the only submitted-run control namespace.
It does not contain cache records and is never consulted for result-key or record-ID identity.
One submitted run uses the same run ID beneath `launcher/v1/runs/` and `views/runs/`.

The launcher allocates a UUID4 run ID under a storage-root allocation guard before starting an orchestrator.
Allocation rejects an existing launcher control directory or canonical run view with that ID.
Attached and submitted run-view creation use the same allocation guard when claiming a preallocated ID, so neither namespace can race into a duplicate execution identity.
The allocation guard is held only for path claims and is never held while a process, task, or filesystem transfer is awaited.

The complete control tree is:

```text
launcher/v1/
  .allocation.guard
  runs/<run-id>/
    .control.guard
    submission.json
    status.json
    progress.jsonl
    execution.claim
    claims/
      <epoch>.json
    cancel_requested
    error.json
    command.json
    local_process.json
    local_process_exit.json
    psij_intent.json
    psij_job.json
    psij/
      executor/
    logs/
      orchestrator.out
      orchestrator.err
    inputs/
      <input-id>.parquet
    return/
      manifest.json
      dataframes/
        <frame-id>.parquet
      assets/
        <digest-shard>/<digest>/...
```

`submission.json`, revision-zero `status.json`, and an empty `progress.jsonl` are created before `submit_workflow()` returns.
`submission.json` is immutable after allocation.
`status.json` is guarded, revisioned mutable state.
`progress.jsonl` is append-only and contains complete newline-terminated canonical JSON objects; readers ignore one unterminated final line left by a crashed writer and reject malformed complete lines.
`execution.claim` exists after an orchestrator claims startup, and each superseded claim is copied to `claims/<epoch>.json` before replacement.
The cancellation marker is a wake-up hint only; `status.json` is authoritative.
`error.json`, `command.json`, local-process identity and exit observations, PSI/J intent and native-job receipt, logs, externalized inputs, and return data exist only for the states or launcher modes that require them.
Immutable JSON artifacts are staged, synchronized where supported, and installed atomically; an exact retry is idempotent and different content is rejected.
`local_process.json` records the PID and operating-system process-birth token required to reconnect safely without signaling a reused PID.
`local_process_exit.json` records the observed exit code and time.
`psij_intent.json` is installed before the one external scheduler submission and contains the run-correlated submit token and exact safe semantic job description.
An optional `bootstrap/psij-pre-launch.sh` is a read-only run-owned artifact whose relative path, size, digest, source kind, optional cluster source path, and optional expected digest are recorded in `submission.json`; the intent binds only its verified confined absolute path, size, and digest.
`psij_job.json` is installed immediately after successful PSI/J submission and correlates that intent with the scheduler-native job ID.
An intent without a receipt is a durable uncertain-submission boundary and is never automatically submitted again.
`psij/executor/` is the fixed shared PSI/J executor work directory reused by submission and later attachment.
The `return/` directory does not exist until a complete staged sibling has been validated and atomically installed.

The control schemas and exact required fields are:

- `bioimageflow.launcher.submission.v2`: `schema`, `run_id`, `created_at`, `storage_root`, `canonical_view`, `workflow`, `invocation`, `parsl_config`, `executor_bindings`, `node_routes`, `environment_routes`, `shared_runtime_root`, `task_policy`, `launch`, `psij_pre_launch`, and `protocol_versions`.
- `bioimageflow.launcher.status.v1`: `schema`, `run_id`, `state`, `revision`, `created_at`, `updated_at`, `backend`, `orchestrator`, `claim_epoch`, `cancel_requested_at`, `hard_termination_requested`, and `error`.
- `bioimageflow.launcher.claim.v1`: `schema`, `run_id`, `owner`, `backend`, `nonce`, `epoch`, `created_at`, `heartbeat_at`, and `expires_at`.
- `bioimageflow.launcher.progress.v1`: `schema`, `run_id`, `sequence`, `timestamp`, `kind`, and `payload`.
- `bioimageflow.launcher.error.v1`: `schema`, `run_id`, `code`, `exception_type`, `message`, `traceback`, `node`, `task`, and `backend`.
- `bioimageflow.launcher.command.v1`: `schema`, `run_id`, `argv`, `work_dir`, and `secret_refs`.
- `bioimageflow.launcher.local_process.v1`: `schema`, `run_id`, `pid`, `start_token`, and `started_at`.
- `bioimageflow.launcher.local_process_exit.v1`: `schema`, `run_id`, `returncode`, and `observed_at`.
- `bioimageflow.launcher.psij_intent.v2`: `schema`, `run_id`, `submit_token`, `created_at`, `executor`, `executor_work_dir`, and `job`, whose optional `pre_launch` descriptor contains the verified absolute run-owned path, size, and digest.
- `bioimageflow.launcher.psij_job.v1`: `schema`, `run_id`, `submit_token`, `executor`, `native_id`, `created_at`, and `executor_work_dir`.
- `bioimageflow.launcher.return.v1`: `schema`, `run_id`, `shape`, `mapping_keys`, `frames`, `root_outputs`, and `locators`.

Schema readers require exactly the versioned fields defined above and reject unknown or missing fields.
Optional values are represented by JSON `null`; fields are not conditionally omitted.
All JSON is UTF-8, finite-value JSON with sorted object keys.
Configuration mappings and ordered return keys preserve their explicitly recorded order where order is part of the public result.
PSI/J backend progress payloads contain exactly `schema`, `event`, `executor`, `native_id`, `state`, and `message`.
Their event is one of `psij_queued`, `psij_active`, `psij_completed`, `psij_failed`, `psij_cancelled`, `psij_unknown`, or `psij_submission_uncertain`.
Scheduler state and native ID remain secondary metadata and never extend the launcher state set or public workflow progress statuses.

`submission.workflow` contains exactly `kind`, `digest`, and `payload`.
The payload is the recursive graph-v1 object or archive-v1 envelope.
`storage_root` is the normalized absolute runtime root assigned by the launcher and is deliberately outside the workflow payload.
It never enters a recursive graph or portable archive, and the orchestrator supplies it explicitly when materializing the workflow.
`canonical_view` is the confined relative path `views/runs/<run-id>`.

Root scalar inputs use a strict recursive typed constant codec.
Path values are normalized absolute paths at submission.
Root DataFrame inputs are written beneath `inputs/` with their stable input-port ID, canonical logical schema and digest, Parquet transport digest, and confined relative path recorded in the invocation.
The orchestrator verifies transport and logical digests before acquiring a DFK.

The exact launcher states are `prepared`, `starting`, `running`, `finalizing`, `cancel_requested`, `succeeded`, `failed`, `cancelled`, and `lost`.
Legal transitions are `prepared` to `starting`, `cancelled`, or `failed`; `starting` to `running`, `cancel_requested`, `failed`, or `lost`; `running` to `cancel_requested`, `finalizing`, `failed`, or `lost`; `finalizing` to `succeeded`, `failed`, or `lost`; and `cancel_requested` to `cancelled`, `failed`, or `lost`.
Terminal status and terminal metadata are immutable.
Every mutation checks the expected revision and, after startup claim, the expected claim epoch under the control-directory guard before atomically replacing `status.json`.
Progress sequence allocation and append occur under the same guard, producing globally monotonic sequences without duplicate allocation.

An execution claim is a renewable lease, not a lock.
Its timestamps are UTC and its expiry is evaluated against the reader's wall clock with backend-absence confirmation required before recovery.
A live unexpired lease excludes another orchestrator.
An expired claim may be replaced before `starting` commits only when the backend is confirmed absent.
After `starting`, recovery never reruns workflow code; it may complete an already installed finalization or record `lost`.

Every persisted relative path is normalized POSIX syntax and confined beneath the control directory or the explicitly named canonical run view.
Readers reject absolute relative-path fields, empty or dot segments, `..`, backslashes, symlink components, and resolved targets outside the named root.
The normalized absolute `storage_root`, `shared_runtime_root`, external input paths, declared external return references, PSI/J semantic job paths, and fixed PSI/J executor work directory are the only absolute path fields.

The public return is staged as a unique sibling, fully written, synchronized where supported, validated, and atomically renamed to `return/`.
`manifest.json` is its commit marker.
Every frame records its confined Parquet path, canonical logical schema and digest, transport digest, and index metadata.
Record-backed path cells name the exact result key, immutable record ID, and record-relative manifest asset.
Declared external paths use external-reference locators.
Run-transient owned paths and durable shared-array values are copied to content-digested `return/assets/` entries and use return-asset locators.
Readers validate immutable record manifests directly and never consult `current.json`.

Submission metadata, inputs, terminal status and error, claim history, and successful returns remain until explicit run deletion.
Prepared/manual and failed runs are not removed automatically.
Logs and progress may be pruned only by an explicit operation that retains submission, status, terminal error, return, and canonical provenance metadata.
Temporary sibling directories may be removed only after their writer is known dead.
Deleting a submitted run is an operator-owned retention action, not a library API.
It removes the launcher control tree only after the run is terminal and no reader or backend monitor is active; canonical records and portable run views follow their own separately coordinated retention operations.

## Human-Facing Run Views

Each workflow execution creates a run directory:

```text
views/runs/<run-id>/
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
- Effective execution backend after explicit engine-injection precedence.
- Effective parallel or sequential scheduling policy.
- BioImageFlow version.
- Requested target nodes.
- Overall status.

`nodes/<node-key>/result.json` records the selected record used by that node in that run:

```json
{
  "schema": "bioimageflow.run.node_result.v1",
  "run_id": "run_a791366f6a8e4fc6a7428bf8e69b57c1",
  "node_key": "segmentation",
  "result_key": "rk_...",
  "record_id": "rec_...",
  "cache_hit": true,
  "canonical": "../../../../cache/v1/results/ab/cd/rk_.../records/rec_...",
  "provenance": {
    "kind": "processing_tool",
    "tool": {
      "module": "example_tools",
      "class": "Segment",
      "version": "1.2.0"
    },
    "logical_digest": "...",
    "environment_hash": "...",
    "parameters": {},
    "inputs": {}
  }
}
```

`record.bioimageflow-link.json` is a pointer file to the selected canonical record.
`outputs/` may contain pointer files to individual user-facing artifacts for convenient browsing.
Only `owned_asset` manifest entries create output pointer files; `external_path` and `scalar_output` entries remain metadata in `result.json`.

Run-node validation resolves and validates the exact immutable record named by `result.json`.
It does not require that record to remain selected by `current.json`.

Example:

```text
views/runs/<run-id>/nodes/segmentation/record.bioimageflow-link.json
views/runs/<run-id>/nodes/segmentation/outputs/mask.tif.bioimageflow-link.json
```

The run view records what the run used, including cache hits from previous runs.
It must not be used to decide cache hits.

## Latest View

`views/latest/` is a mutable per-node convenience view.
Each entry points to the latest successful result for that node, even if the workflow run later fails on another node.

```text
views/latest/<node-key>.bioimageflow-link.json
```

The latest fully successful workflow run is tracked separately with a pointer file:

```text
views/runs/latest-success.bioimageflow-link.json
```

The latest view should be updated after the run view has been written.
Updating `views/latest/<node-key>` must be atomic:

1. Create a temporary pointer file in the same parent directory.
2. Atomically replace the existing latest entry with the temporary entry.

If a workflow run fails or is cancelled, the engine may still update `views/latest/<node-key>` for nodes that successfully resolved to a selected record before the failure.
It must not update `views/runs/latest-success.bioimageflow-link.json` unless the whole requested workflow run completed successfully.

The latest view is allowed to change over time.
It must not be included in result-key hashing.

## Pointer Files and Symlink Export

The engine writes pointer files by default for run and latest views.
This avoids platform-specific symlink permissions and keeps the public run view portable.

Directory pointer example:

```text
views/runs/<run-id>/nodes/<node-key>/record.bioimageflow-link.json
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
views/runs/<run-id>/nodes/<node-key>/outputs/mask.tif.bioimageflow-link.json
```

```json
{
  "schema": "bioimageflow.link.v1",
  "kind": "file",
  "target": "../../../../../cache/v1/results/ab/cd/rk_.../records/rec_.../assets/mask.tif",
  "digest": "sha256:..."
}
```

Pointer files must be written by temp-file plus atomic rename.
Pointer targets should use normalized relative paths when possible.
Output pointer targets resolve to the selected canonical record's owned assets, not to attempt staging directories or a separate run-local record copy.
The canonical run view preserves the record-relative asset path from the selected manifest, so a manifest asset at `assets/mask.tif` is exposed as `outputs/assets/mask.tif.bioimageflow-link.json` beneath `views/runs/<run-id>/nodes/<node-key>/`.

`Workflow(storage_path=..., output_view=...)`, `Workflow.export_outputs(...)`, and `bioimageflow.export_outputs(...)` may materialize complete node exports under `outputs/runs/` and `outputs/latest/`.
`OutputView.mode` supports `none`, `pointer`, `symlink`, `copy`, and `hardlink`.
`none` creates no disposable `outputs/` projection and leaves only the canonical portable JSON pointers under `views/`.
`pointer` creates portable `*.bioimageflow-link.json` files under `outputs/`; their relative targets are validated and confined to the workflow storage root.
`symlink`, `copy`, and `hardlink` use the corresponding filesystem operations.
`OutputView.scope` accepts `latest`, `runs`, or `both`.

The `outputs/runs/<run-id>/nodes/<node-key>/outputs/` hierarchy is record-relative for every materialization mode.
For example, `assets/mask.tif` is materialized there as `outputs/assets/mask.tif` or, in pointer mode, `outputs/assets/mask.tif.bioimageflow-link.json`.
The node output directory also contains `dataframe.parquet`, `dataframe.csv`, `dataframe.json`, and `provenance.json`.

For an owned directory, `pointer` creates one directory pointer, `symlink` links the directory root, `copy` recursively copies its validated tree, and `hardlink` creates directories and hard-links each regular file.
The `hardlink` mode never attempts to hard-link a directory itself.
All modes validate the complete destination mapping before writing and reject symlink or path escape in the source tree.

The `outputs/latest/` hierarchy uses a human-facing mapping beneath the node layer:

```text
records/<record-id>/assets/t051.tiff
  -> outputs/latest/<node-key>/t051.tiff

records/<record-id>/assets/masks/nuclei/t051.tiff
  -> outputs/latest/<node-key>/masks/nuclei/t051.tiff
```

Exactly one leading `assets/` is stripped.
Everything after it is preserved, while a safe manifest path that does not begin with `assets/` is mapped in full beneath the node directory.
Scoped node paths used by recursive workflows form part of the node layer beneath `outputs/latest/`.
The complete mapped path set is validated before materialization, including collision and file/directory-prefix checks.
The names `dataframe.parquet`, `dataframe.csv`, `dataframe.json`, and `provenance.json` are reserved at the root of each latest node export.

Each latest node tree is built and fully materialized in a temporary sibling directory before installation.
Installation publishes exactly the complete mapped output set for that node.
If materialization or installation fails, the currently published node tree stays usable or is restored, and temporary directories are cleaned.
This is a failure-safe installation guarantee, not a promise of filesystem-level atomic directory replacement on every host filesystem.

> outputs/latest contains the latest successful output independently for each node. Nodes may refer to different workflow executions after selected, failed, cancelled, or overlapping runs.

Linked outputs refer to canonical cached files and should be treated as read-only.
Copied outputs are independent but require additional storage.
Materialized files are disposable human-facing outputs and are not canonical cache records.

The selected record's canonical `dataframe.parquet` follows the requested pointer, symlink, copy, or hardlink mode.
The readable `dataframe.csv` and versioned split-orientation `dataframe.json` are derived regular files.
In the latest view, their owned-asset values are rewritten to the same paths produced by stripping the leading `assets/`.
`provenance.json` consolidates run metadata, node computation metadata, selected upstream record references and selectors, record identity, dataframe metadata, and declared outputs.
This overlap with `run.json`, node `result.json`, and record `manifest.json` is deliberate because a copied export must remain explainable outside the canonical cache tree.

`Storage.probe_output_view_mode(mode: str) -> OutputViewCapability` probes beneath the actual `Storage.storage_path` and returns one of the stable diagnostic codes `ok`, `permission_denied`, `filesystem_unsupported`, `invalid_mode`, or `io_error`.
The probe verifies both file and directory symlinks are readable, tests hardlinks only with files, exercises copy and pointer creation, and cleans its artifacts without changing cache or output selection state.
The library does not define an automatic fallback mode; platform callers choose fallback policy.

`Workflow.export_outputs(...)` and `bioimageflow.export_outputs(...)` are explicit operations and raise if the requested materialization cannot be produced.
The top-level function defaults to a latest copy beneath the storage path, making it suitable for the CLI:

```bash
bioimageflow export-outputs <storage-path>
bioimageflow export-outputs <storage-path> --mode copy --scope runs --run-id <run-id>
```

Passing `destination=<path>` or `--destination <path>` installs a complete external output root containing `latest/` and/or `runs/<run-id>/`.
External exports are always rebuilt from canonical run views and immutable records, never from a disposable tree already present under `outputs/`.
The complete tree is staged beside the destination before installation.
An existing destination raises `FileExistsError` unless `replace=True` or `--replace` is provided; failed replacement restores the previous tree and removes staging artifacts.
Passing `replace=True` without an explicit destination raises `ValueError`.
The destination may not equal, contain, or be contained by the source storage root.
Returned paths always identify the installed destination rather than its staging directory.

Automatic disposable output-view materialization logs a warning and does not turn a successfully computed workflow into a failed computation.

## Invalidation, Retention, and Transient Cleanup

V1 invalidation changes cache selection state; it does not delete record directories automatically.
Invalidating a node removes or tombstones `current.json` for affected result keys and downstream result keys according to workflow dependency rules.
Invalidation must use the same per-result-key guarded metadata update discipline as publication, or coordinate externally with active compute operations for the same workflow storage path.

V1 core does not define a background garbage collector.
An explicit `cleanup_transients()` operation may remove transient, non-canonical state:

- Stale incomplete attempts.
- Failed attempts.
- Stale temporary record directories.
- Terminal non-reusable invocation directories under `cache/v1/transient/runs/`.

`cleanup_transients()` must not delete `records/<record-id>/` directories.
Published-record pruning is an explicit user operation and requires its own retention policy, dry-run behavior, and active-reader protection.

Transient cleanup may remove an attempt, temporary record directory, or non-reusable invocation only when both are true:

- The target is older than the configured stale threshold.
- The caller has established that no compute or publish operation can still be using it, either by external coordination or by a lease mechanism.

Automatic record pruning is not part of the cache contract.

## Cluster Submission Staging

Laptop-to-cluster submission uses a separate operator-selected transport staging root.
That root must be disjoint from `Workflow.storage_path` and therefore from every cache, launcher, run-view, and output-view path described in this document.
It contains partial transfers, committed upload trees, content-addressed read-only input objects, and operation receipts only.
It is not canonical storage, a cache, a launcher repository, or a remote run store.

An installed input object is addressed by its canonical upload-manifest digest rather than by a launcher run ID.
The orchestrator and all selected Parsl workers must retain access to that object for every referencing run.
Sites may remove incomplete partial transfers after a local stale threshold and may remove an object only after proving that no live or retained launcher submission references it.
BioImageFlow does not provide an automatic transport garbage collector.

## Shared Memory Interaction

Shared memory is a runtime transport optimization, not a canonical storage format.

Shared-memory segment names must not appear in result keys or record IDs.
Records must contain durable file artifacts or manifests that can be read by a different process or machine.

If a source or column-bound `ProcessingTool` produces a `SharedArray` during execution and the result is cacheable, the engine must publish a durable representation before a cache hit can be served to a later run.
The durable representation belongs in the selected record and is described by the manifest.

V1 represents reusable shared-memory outputs as record-owned assets.
Column-bound outputs produce one owned asset for each non-null `SharedArray` dataframe cell.
For example, a shared-memory dataframe cell may be canonicalized to:

```text
assets/shm/<safe-column>/<row-position>_<safe-row>.npy
```

The `assets/shm/` namespace is reserved for shared-array assets; ordinary path outputs must use another `assets/` subdirectory.
The safe column and row segments are sanitized and digest-suffixed to avoid unsafe paths and collisions.
The manifest records the array dtype, shape, order, and digest.
The owned asset entry uses `asset_role: "shared_array"` and an `array` object with the producing dataframe `column`, stringified `row_index`, `format: "npy"`, `dtype`, `order`, and `shape`.
For example:

```json
{
  "kind": "owned_asset",
  "asset_role": "shared_array",
  "path": "assets/shm/result_a791366f/000000_row_ab12cd34.npy",
  "size": 192,
  "digest": "sha256:...",
  "array": {
    "column": "result",
    "row_index": "row",
    "format": "npy",
    "dtype": "uint8",
    "order": "C",
    "shape": [2, 2]
  }
}
```

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

## Storage Summary

The storage layout separates logical computation identity, execution attempts, immutable records, and human-facing views:

```text
cache/v1/results/<result-shard>/<result-key>/
  attempts/<attempt-id>/
  records/<record-id>/
  current.json

cache/v1/transient/runs/<run-id>/nodes/<node-key>/<invocation-id>/
launcher/v1/runs/<run-id>/
views/runs/<run-id>/
views/latest/
outputs/runs/
outputs/latest/
```

`docs/source/specs.md` summarizes this contract and should remain aligned with this reference document.
