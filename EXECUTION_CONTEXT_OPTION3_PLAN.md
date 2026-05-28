# ExecutionContext Option 3 Migration

## Target Contract

This branch intentionally breaks the old `ExecutionContext.work_dir` meaning.

`ExecutionContext` should expose:

- `run_dir`: node execution root, `storage_path/data/<node>/<timestamp>_<hash12>/`
- `assets_dir`: declared output root, `run_dir/assets/`
- `work_dir`: node-level shared scratch root, `run_dir/work/`
- `rows_dir`: per-row scratch parent, `run_dir/work/rows/`
- `row_dir`: private scratch directory for one `process_row` call, `run_dir/work/rows/<safe_row_id>/`
- `batch_dir`: private scratch directory for one `process_batch` call, `run_dir/work/batch/`
- `row_index`: original input row index for `process_row`, otherwise `None`

Tools should use:

- `context.work_dir` for files shared by all row calls in the same node run.
- `context.row_dir` as `cwd` for row-level external commands.
- `context.batch_dir` as `cwd` for batch-level external commands.
- `context.assets_dir` only for declared outputs resolved through `Arguments`.

## Worktrees

- Integration: `/private/tmp/bioimageflow-context-option3` (`context-option3`)
- Core context/worker slice: `/private/tmp/bioimageflow-context-option3-core` (`context-option3-core`)
- Engine slice: `/private/tmp/bioimageflow-context-option3-engine` (`context-option3-engine`)
- Tools slice: `/private/tmp/bioimageflow-context-option3-tools` (`context-option3-tools`)
- Docs slice: `/private/tmp/bioimageflow-context-option3-docs` (`context-option3-docs`)

## Test Matrix

Targeted tests:

```bash
uv run pytest tests/unit/test_worker_execution_context.py \
  tests/integration/test_execution_context.py \
  tests/integration/test_runtime_paths.py \
  tests/unit/test_atlas_workdir.py \
  tests/unit/test_worker_timeout.py
```

Full checks:

```bash
uv run pytest
uv run ruff check .
```

## Integration Notes

- Merged core first, then engine, then tools, then docs into `context-option3`.
- No backward-compatibility aliases were kept for the old per-row `work_dir` behavior.
- `ExecutionContext.__post_init__` now rejects old-shaped contexts where `work_dir`
  points at a row or batch directory.
- Atlas fallback reference generation now uses `work/atlas/blobs.txt`, writes via
  a temporary file plus atomic replace, and serializes parallel generation with a
  lock directory.
- The package-local `bioimageflow_common_tools/data/blobs.txt` fixture is tracked
  because `bioimageflow-common-tools` already force-includes it in its package
  metadata.

## Verification

Passed:

```bash
uv run pytest tests/unit/test_worker_execution_context.py \
  tests/integration/test_execution_context.py \
  tests/integration/test_runtime_paths.py \
  tests/unit/test_atlas_workdir.py \
  tests/unit/test_worker_timeout.py
```

Result: `28 passed`.

Passed:

```bash
uv run ruff check .
git diff --check
```

Full suite:

```bash
uv run pytest
```

Result: `814 passed, 1 failed`. The remaining failure is unrelated to this
migration: `tests/unit/test_validation_serialization.py::test_common_tool_image_fields_use_imagefile_without_converting_plain_paths`
expects `Mosaic.Outputs.mosaic_path` to serialize as `Path`, but the current
clean branch declares it with `ImageSpec`, so it serializes as `ImageFile`.

## Final Search

- Search before final review:

```bash
rg -n "context\\.work_dir|work_dir.*per-row|work_dir.*batch|ExecutionContext\\(" packages tests docs example-workflows
```
