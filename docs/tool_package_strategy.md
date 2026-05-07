# Tool Package Strategy

BioImageFlow tools and reusable workflows are moving into package-owned
boundaries. Each package owns its public tools, workflows, examples, tests,
fixtures, and authoring docs.

## Rules

- Do not add backward compatibility layers for the migration. Update imports,
  examples, and docs to the new package names.
- Examples must use the new packages directly. Avoid legacy in-repo paths and
  workflow-local examples for reusable tools.
- Every public tool requires tests for schema serialization, binding
  validation, one successful execution path, output templates, and expected
  failure cases.
- Every public workflow or sub-workflow requires tests for graph construction,
  public input/output contract, serialization where supported, and one
  end-to-end run on public or synthetic data.
- Package agents own the docs for the package they change. A package PR is not
  complete until its examples and authoring notes match the shipped API.
- `ProcessingTool` dependencies belong inside `process_row` or
  `process_batch`; package modules should import only the standard library and
  `bioimageflow-core` at import time.
- Use `DataFrameTool` only for main-process dataframe operations.

## Phase 3 Examples

Phase 3 should include package-backed example workflows for:

- Spot detection: load public or synthetic microscopy data, detect spots,
  measure per-image/per-object counts, and export a compact result table.
- Restoration: apply a restoration or denoising tool to public or synthetic
  images, validate output image shape/type, and compare a deterministic metric.
- Tracking: link detections across frames on public or synthetic time-lapse
  data, validate track IDs, and export a track table.

Each example must run from documented package imports, use reproducible data,
and have an automated test that exercises the smallest meaningful workflow.
