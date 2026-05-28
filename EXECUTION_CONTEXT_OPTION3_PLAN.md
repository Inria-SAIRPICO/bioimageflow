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

- Merge core first, then engine, then tools, then docs.
- Do not keep backward-compatibility aliases for the old per-row `work_dir` behavior.
- Search before final review:

```bash
rg -n "context\\.work_dir|work_dir.*per-row|work_dir.*batch|ExecutionContext\\(" packages tests docs example-workflows
```

