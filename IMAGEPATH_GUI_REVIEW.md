# ImagePath GUI Review

## Findings

1. `packages/bioimageflow-common-tools/bioimageflow_common_tools/atlas.py:48` and `packages/bioimageflow-common-tools/bioimageflow_common_tools/atlas.py:83` still declare image fields as `Annotated[Path, ImageSpec(...), GUIMeta(...)]`. These should use `ImagePath(..., gui=GUIMeta(...))` so common tools consistently exercise the new factory support while preserving the same `ImageSpec` constraints and GUI metadata.

2. `packages/bioimageflow-common-tools/bioimageflow_common_tools/mosaic.py:37` still declares the image input as `Annotated[Path, ImageSpec(...), GUIMeta(...)]`. It should use `ImagePath(..., gui=GUIMeta(...))`. `mosaic_path` at `packages/bioimageflow-common-tools/bioimageflow_common_tools/mosaic.py:63` has no `ImageSpec` today, so this review leaves it as a plain `Path` to avoid inventing new type metadata.

3. `packages/bioimageflow-common-tools/bioimageflow_common_tools/stardist_segmenter.py:42` and `packages/bioimageflow-common-tools/bioimageflow_common_tools/stardist_segmenter.py:95` still declare image fields as `Annotated[Path, ImageSpec(...), GUIMeta(...)]`. These should use `ImagePath(..., gui=GUIMeta(...))` with unchanged semantics and layouts.

4. `packages/bioimageflow-common-tools/bioimageflow_common_tools/label_overlaps.py:38` and `packages/bioimageflow-common-tools/bioimageflow_common_tools/label_overlaps.py:47` are already migrated to `ImagePath`. The CSV output at `packages/bioimageflow-common-tools/bioimageflow_common_tools/label_overlaps.py:58` correctly remains a plain `Path`.

## Validation Plan

- Re-scan common tools for remaining `ImageSpec` imports / `Annotated[Path, ImageSpec(...)]` image declarations after fixes.
- Run focused unit tests covering type factories, schema serialization, validation helpers, and common-tool schema JSON serialization.

## Resolution

- Migrated the remaining common-tool image fields in `atlas.py`, `mosaic.py`, and `stardist_segmenter.py` to `ImagePath(..., gui=GUIMeta(...))`.
- Preserved the existing semantics, layouts, formats, templates, and GUI metadata values.
- Left plain path fields unchanged, including `LabelOverlaps.overlaps`, `Mosaic.mosaic_path`, and the file-listing path fields.
- Added a regression test asserting the migrated common-tool image fields serialize as `ImagePath` while plain path outputs still serialize as `Path`.

## Validation Results

- `rg -n "ImageSpec|Annotated\[\s*Path|Annotated\[Path" packages/bioimageflow-common-tools/bioimageflow_common_tools`
  - Only plain `Annotated[Path, GUIMeta(...)]` fields remain.
- `git diff --check`
  - Passed.
- `uv run pytest tests/unit/test_types.py tests/unit/test_validation.py tests/unit/test_validation_serialization.py tests/integration/test_type_compatibility.py`
  - Blocked before test collection because `wetlands==1.0.1 @ editable+wetlands-lib` points to a missing local path.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=packages/bioimageflow-core:packages/bioimageflow:packages/bioimageflow-common-tools /Library/Frameworks/Python.framework/Versions/3.12/bin/pytest tests/unit/test_types.py tests/unit/test_validation.py tests/unit/test_validation_serialization.py tests/integration/test_type_compatibility.py`
  - Passed: 176 tests.
