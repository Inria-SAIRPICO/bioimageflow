# Tool Packages

BioImageFlow tool packages are optional distributions that group tools by
workflow domain. Package-owned documentation lives beside each package so the
tool code, tests, demo-data notes, and user docs evolve together.

Each package and each public tool must have a detailed description covering
purpose, core libraries or binaries, inputs, outputs, assumptions, expected
results, minimal examples, and failure modes.

Package-local tests can be run directly:

```bash
uv run pytest packages
uv run pytest packages/bioimageflow-io-tools/tests
uv run pytest -m package_tools
```

The root test suite also discovers package-local tests through the repository
pytest configuration.

## Release and CI Contract

The orchestrator, core package, and companion tool packages are released with lockstep versions so workflow examples and package docs describe one coherent BioImageFlow distribution.
The orchestrator and first-party tool packages declare Python `>=3.10`; `bioimageflow-core` declares Python `>=3.9` so Wetlands worker environments with Python 3.9 binary dependencies can install the shared worker API.
The deterministic CI matrix validates the main development/runtime surface on Python 3.10, 3.11, and 3.12, while static compatibility tests keep `bioimageflow-core` import syntax compatible with Python 3.9.

Package metadata separates distribution dependencies from isolated runtime dependencies.
Install-time dependencies must stay small enough for package import, documentation discovery, and metadata validation.
Heavy or tool-specific runtimes belong in the tool's `EnvironmentSpec`, not in the package import path.
Package-local `uv.sources` entries mirror first-party runtime dependencies so editable workspace runs and built artifacts use the same package graph.

The regular CI gate for package and documentation changes includes:

```bash
uv run ruff check .
uv run pyright
uv run pytest -m "not slow"
uv run pytest tests/unit/test_package_artifacts.py
uv build --all-packages --out-dir dist/packages
uv run sphinx-build -W --keep-going docs/source docs/_build/html
```

The complete-test jobs are manual or scheduled because they may require optional model runtimes, Wetlands environment creation, or heavier package fixtures.
Those jobs are useful release evidence, but deterministic unit, package-artifact, and documentation checks remain the required proof for ordinary package changes.

Wheels exclude package documentation, package tests, generated build outputs, and local caches.
Source distributions keep package docs and tests so release artifacts remain auditable without bloating installed wheels.
Release metadata must not expose broad extras that silently install all domain runtimes; users install the companion packages and isolated tool environments they actually need.
Publishing is currently manual and outside CI deployment.
Release operators must publish only artifacts produced after the deterministic gates above pass; CI intentionally builds and stores artifacts but does not upload them to an index.

## Package-Owned Documentation

The sections below include the package-owned Markdown files from
`packages/*/docs`. The source of truth remains in each package directory.

```{include} ../../../packages/bioimageflow-common-tools/docs/index.md
:relative-docs: ../../../packages/bioimageflow-common-tools/docs/
```

```{include} ../../../packages/bioimageflow-io-tools/docs/index.md
:relative-docs: ../../../packages/bioimageflow-io-tools/docs/
```

```{include} ../../../packages/bioimageflow-measurement-tools/docs/index.md
:relative-docs: ../../../packages/bioimageflow-measurement-tools/docs/
```

```{include} ../../../packages/bioimageflow-segmentation-tools/docs/index.md
:relative-docs: ../../../packages/bioimageflow-segmentation-tools/docs/
```

```{include} ../../../packages/bioimageflow-sairpico-tools/docs/index.md
:relative-docs: ../../../packages/bioimageflow-sairpico-tools/docs/
```

```{include} ../../../packages/bioimageflow-spot-tools/docs/index.md
:relative-docs: ../../../packages/bioimageflow-spot-tools/docs/
```

```{include} ../../../packages/bioimageflow-restoration-tools/docs/index.md
:relative-docs: ../../../packages/bioimageflow-restoration-tools/docs/
```

```{include} ../../../packages/bioimageflow-tracking-tools/docs/index.md
:relative-docs: ../../../packages/bioimageflow-tracking-tools/docs/
```

## Tool Pages

## Common Public Tools

```{include} ../../../packages/bioimageflow-common-tools/docs/tools/files.md
```

```{include} ../../../packages/bioimageflow-common-tools/docs/tools/table_from_csv.md
```

```{include} ../../../packages/bioimageflow-common-tools/docs/tools/generate.md
```

```{include} ../../../packages/bioimageflow-common-tools/docs/tools/inner_join.md
```

```{include} ../../../packages/bioimageflow-common-tools/docs/tools/cross_join.md
```

```{include} ../../../packages/bioimageflow-common-tools/docs/tools/join_on_column.md
```

```{include} ../../../packages/bioimageflow-common-tools/docs/tools/concat.md
```

```{include} ../../../packages/bioimageflow-common-tools/docs/tools/collect.md
```

```{include} ../../../packages/bioimageflow-common-tools/docs/tools/filter_table_rows.md
```

```{include} ../../../packages/bioimageflow-common-tools/docs/tools/select_columns.md
```

```{include} ../../../packages/bioimageflow-common-tools/docs/tools/write_table.md
```

```{include} ../../../packages/bioimageflow-common-tools/docs/tools/extract_channel.md
```

```{include} ../../../packages/bioimageflow-common-tools/docs/tools/connected_components.md
```

```{include} ../../../packages/bioimageflow-common-tools/docs/tools/label_overlaps.md
```

```{include} ../../../packages/bioimageflow-common-tools/docs/tools/mosaic.md
```

## Specialized Wrapper Pages

These pages document specialized module-level wrappers that are available from
their package modules but are not common-tool root exports.

```{include} ../../../packages/bioimageflow-common-tools/docs/tools/atlas.md
```

```{include} ../../../packages/bioimageflow-common-tools/docs/tools/convert_image.md
```

## IO Tools

```{include} ../../../packages/bioimageflow-io-tools/docs/tools/read_image.md
```

```{include} ../../../packages/bioimageflow-io-tools/docs/tools/read_image_metadata.md
```

```{include} ../../../packages/bioimageflow-io-tools/docs/tools/validate_image_layout.md
```

```{include} ../../../packages/bioimageflow-io-tools/docs/tools/convert_image_format.md
```

```{include} ../../../packages/bioimageflow-io-tools/docs/tools/convert_to_ome_tiff.md
```

```{include} ../../../packages/bioimageflow-io-tools/docs/tools/convert_to_ome_zarr.md
```

```{include} ../../../packages/bioimageflow-io-tools/docs/tools/select_scene.md
```

```{include} ../../../packages/bioimageflow-io-tools/docs/tools/select_timepoint.md
```

```{include} ../../../packages/bioimageflow-io-tools/docs/tools/select_channel.md
```

```{include} ../../../packages/bioimageflow-io-tools/docs/tools/select_z_range.md
```

```{include} ../../../packages/bioimageflow-io-tools/docs/tools/select_dimensions.md
```

## Measurement Tools

```{include} ../../../packages/bioimageflow-measurement-tools/docs/tools/region_properties.md
```

```{include} ../../../packages/bioimageflow-measurement-tools/docs/tools/shape_properties.md
```

```{include} ../../../packages/bioimageflow-measurement-tools/docs/tools/intensity_properties.md
```

```{include} ../../../packages/bioimageflow-measurement-tools/docs/tools/count_labels.md
```

```{include} ../../../packages/bioimageflow-measurement-tools/docs/tools/summarize_table.md
```

```{include} ../../../packages/bioimageflow-measurement-tools/docs/tools/label_benchmark.md
```

```{include} ../../../packages/bioimageflow-measurement-tools/docs/tools/dice_iou.md
```

```{include} ../../../packages/bioimageflow-measurement-tools/docs/tools/object_matching_metrics.md
```

```{include} ../../../packages/bioimageflow-measurement-tools/docs/tools/aggregate_per_image.md
```

```{include} ../../../packages/bioimageflow-measurement-tools/docs/tools/normalize_features.md
```

## Segmentation Tools

```{include} ../../../packages/bioimageflow-segmentation-tools/docs/tools/threshold_segment.md
```

```{include} ../../../packages/bioimageflow-segmentation-tools/docs/tools/otsu_threshold_segment.md
```

```{include} ../../../packages/bioimageflow-segmentation-tools/docs/tools/local_threshold_segment.md
```

```{include} ../../../packages/bioimageflow-segmentation-tools/docs/tools/watershed_segment.md
```

```{include} ../../../packages/bioimageflow-segmentation-tools/docs/tools/distance_watershed_segment.md
```

```{include} ../../../packages/bioimageflow-segmentation-tools/docs/tools/split_touching_objects.md
```

```{include} ../../../packages/bioimageflow-segmentation-tools/docs/tools/filter_labels.md
```

```{include} ../../../packages/bioimageflow-segmentation-tools/docs/tools/postprocess_labels.md
```

```{include} ../../../packages/bioimageflow-segmentation-tools/docs/tools/cellpose3.md
```

```{include} ../../../packages/bioimageflow-segmentation-tools/docs/tools/stardist_segmenter.md
```

## SAIRPICO Tools

```{include} ../../../packages/bioimageflow-sairpico-tools/docs/tools/gaussian_psf.md
```

```{include} ../../../packages/bioimageflow-sairpico-tools/docs/tools/gibson_lanni_psf.md
```

```{include} ../../../packages/bioimageflow-sairpico-tools/docs/tools/richardson_lucy_deconvolution.md
```

```{include} ../../../packages/bioimageflow-sairpico-tools/docs/tools/wiener_deconvolution.md
```

```{include} ../../../packages/bioimageflow-sairpico-tools/docs/tools/spitfire_deconvolution.md
```

```{include} ../../../packages/bioimageflow-sairpico-tools/docs/tools/median_denoising.md
```

```{include} ../../../packages/bioimageflow-sairpico-tools/docs/tools/cimg_denoising.md
```

```{include} ../../../packages/bioimageflow-sairpico-tools/docs/tools/hotspot_detection.md
```

```{include} ../../../packages/bioimageflow-sairpico-tools/docs/tools/hotspot_to_spots.md
```

## Spot Tools

```{include} ../../../packages/bioimageflow-spot-tools/docs/tools/detect_spots.md
```

```{include} ../../../packages/bioimageflow-spot-tools/docs/tools/filter_spots.md
```

```{include} ../../../packages/bioimageflow-spot-tools/docs/tools/render_spots.md
```

```{include} ../../../packages/bioimageflow-spot-tools/docs/tools/spots_to_labels.md
```

```{include} ../../../packages/bioimageflow-spot-tools/docs/tools/spot_colocalization.md
```

```{include} ../../../packages/bioimageflow-spot-tools/docs/tools/spot_quality_metrics.md
```

```{include} ../../../packages/bioimageflow-spot-tools/docs/tools/assign_spots_to_labels.md
```

```{include} ../../../packages/bioimageflow-spot-tools/docs/tools/spot_summary.md
```

## Restoration Tools

```{include} ../../../packages/bioimageflow-restoration-tools/docs/tools/restore_image.md
```

```{include} ../../../packages/bioimageflow-restoration-tools/docs/tools/gaussian_denoise.md
```

```{include} ../../../packages/bioimageflow-restoration-tools/docs/tools/median_denoise.md
```

```{include} ../../../packages/bioimageflow-restoration-tools/docs/tools/background_subtract.md
```

```{include} ../../../packages/bioimageflow-restoration-tools/docs/tools/unsharp_mask.md
```

```{include} ../../../packages/bioimageflow-restoration-tools/docs/tools/richardson_lucy_restoration.md
```

```{include} ../../../packages/bioimageflow-restoration-tools/docs/tools/benchmark_restoration.md
```

## Tracking Tools

```{include} ../../../packages/bioimageflow-tracking-tools/docs/tools/labels_to_objects.md
```

```{include} ../../../packages/bioimageflow-tracking-tools/docs/tools/filter_objects.md
```

```{include} ../../../packages/bioimageflow-tracking-tools/docs/tools/link_objects.md
```

```{include} ../../../packages/bioimageflow-tracking-tools/docs/tools/tracks_to_labels.md
```

```{include} ../../../packages/bioimageflow-tracking-tools/docs/tools/track_table_validate.md
```

```{include} ../../../packages/bioimageflow-tracking-tools/docs/tools/track_summary.md
```

```{include} ../../../packages/bioimageflow-tracking-tools/docs/tools/track_quality_metrics.md
```

```{include} ../../../packages/bioimageflow-tracking-tools/docs/tools/track_metrics.md
```

## Workflow Pages

```{include} ../../../packages/bioimageflow-common-tools/docs/workflows/common_glue.md
```

```{include} ../../../packages/bioimageflow-io-tools/docs/workflows/ome_normalization.md
```

```{include} ../../../packages/bioimageflow-measurement-tools/docs/workflows/object_measurement.md
```

```{include} ../../../packages/bioimageflow-segmentation-tools/docs/workflows/bbbc038_segmentation_benchmark.md
```

```{include} ../../../packages/bioimageflow-sairpico-tools/docs/workflows/sairpico_restoration_smoke.md
```

```{include} ../../../packages/bioimageflow-spot-tools/docs/workflows/puncta_analysis.md
```

```{include} ../../../packages/bioimageflow-restoration-tools/docs/workflows/restoration_benchmark.md
```

```{include} ../../../packages/bioimageflow-tracking-tools/docs/workflows/tracking_analysis.md
```
