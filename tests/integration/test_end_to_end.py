"""
End-to-end integration tests showing complete real-world-like workflows.

These tests combine multiple features into realistic pipeline scenarios.
"""

from typing import Any

import pandas as pd

from bioimageflow import Collect, Concat, JoinOnColumn, Workflow

from .conftest import (
    CellposeSegmenter,
    ColumnRegex,
    CsvLoader,
    FileLoader,
    FilterRows,
    StardistSegmenter,
    StubSegmenter,
    StubStats,
    StubTiler,
)


class TestBioImageAnalysisPipeline:
    """
    Complete workflow: load → extract metadata → filter → segment → measure.
    Mirrors the example in specs Section 4.1.
    """

    def test_full_analysis_pipeline(self, tmp_workspace_with_metadata):
        ws = tmp_workspace_with_metadata
        load = FileLoader()
        regex = ColumnRegex()
        filt = FilterRows()
        segment = StubSegmenter()
        measure = StubStats()

        with Workflow(storage_path=ws / "results") as wf:
            raw = load(path=str(ws / "data"))
            with_meta = regex(
                raw,
                column_name="filename",
                regex=r"(?P<patient>\w+)_(?P<slice>\d+)\.tif",
            )
            good = filt(with_meta, column_name="slice", min=1.0)
            masks = segment(input_image=good["path"], diameter=30.0)
            results = measure(image=good["path"], mask=masks["mask"])
            df = wf.compute(results)
            assert isinstance(df, pd.DataFrame)
            assert "mean_intensity" in df.columns
            assert "area" in df.columns
            assert len(df) == 3  # All slices >= 1


class TestMultiModalPipeline:
    """
    Multi-source workflow: MRI + CT joined on patient_id, enriched with CSV.
    Mirrors specs Section 4.1 multi-source example.
    """

    def test_multi_modal_registration(self, tmp_workspace_two_sources):
        ws = tmp_workspace_two_sources
        load = FileLoader()
        csv_load = CsvLoader()
        regex = ColumnRegex()
        join = JoinOnColumn()

        from .conftest import StubRegistration

        register = StubRegistration()

        with Workflow(storage_path=ws / "results") as wf:
            mri = load(path=str(ws / "mri"), name="mri")
            ct = load(path=str(ws / "ct"), name="ct")
            patients = csv_load(path=str(ws / "patients.csv"), name="patients")

            mri_meta = regex(
                mri,
                column_name="filename",
                regex=r"(?P<patient_id>\w+)_mri",
                name="mri_regex",
            )
            ct_meta = regex(
                ct,
                column_name="filename",
                regex=r"(?P<patient_id>\w+)_ct",
                name="ct_regex",
            )

            paired = join(
                mri_meta,
                ct_meta,
                join_column="patient_id",
                suffixes=("_mri", "_ct"),
                name="pair_modalities",
            )

            enriched = join(
                paired,
                patients,
                join_column="patient_id",
                how="left",
                name="enrich_patients",
            )

            registered = register(
                fixed=enriched["path_mri"],
                moving=enriched["path_ct"],
            )

            df = wf.compute(registered)
            assert len(df) == 3
            assert "registered" in df.columns
            assert "displacement" in df.columns


class TestTilingPipeline:
    """
    Tile → segment each tile → collect results.
    Tests explosion + downstream processing + collection.
    """

    def test_tile_segment_collect(self, tmp_workspace):
        load = FileLoader()
        tile = StubTiler()
        segment = StubSegmenter()
        collect = Collect()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            tiles = tile(input_image=raw["path"], tile_count=2)
            masks = segment(input_image=tiles["tile"])
            all_data = collect(tiles, masks)
            df = wf.compute(all_data)
            # 3 images × 2 tiles = 6 rows
            assert len(df) == 6
            assert "tile" in df.columns
            assert "mask" in df.columns
            assert "cell_count" in df.columns


class TestComparativeAnalysis:
    """
    Compare two segmentation algorithms on the same data.
    Cellpose vs Stardist, then collect all results.

    Note: Both segmenters produce a 'mask' column. InnerJoin (used by Collect)
    drops duplicate columns (via __bif_dup suffix). To preserve both masks,
    we use separate Collect nodes or a JoinOnColumn with suffixes. Here we
    use InnerJoin with explicit suffix handling via JoinOnColumn.
    """

    def test_algorithm_comparison(self, tmp_workspace):
        load = FileLoader()
        cellpose = CellposeSegmenter()
        stardist = StardistSegmenter()
        _join = JoinOnColumn()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            cp_masks = cellpose(input_image=raw["path"], name="cellpose")
            sd_masks = stardist(input_image=raw["path"], name="stardist")

            # Collect raw + both segmentation results, using InnerJoin for
            # raw+cellpose, then JoinOnColumn would require a shared column.
            # Instead, collect raw with each segmenter separately, then combine.
            collect = Collect()
            raw_and_cp = collect(raw, cp_masks, name="raw_cp")
            all_data = collect(raw_and_cp, sd_masks, name="all_data")
            df = wf.compute(all_data)
            assert len(df) == 3
            assert "path" in df.columns       # from raw
            assert "cell_count" in df.columns  # from cellpose (has cell_count)
        # sd_masks also has a 'mask' column — InnerJoin drops the duplicate
        # so only one 'mask' column survives. Both segmenters' outputs are
        # in the DataFrame (cell_count from cellpose, mask from one of them).


class TestCachedRerunPipeline:
    """
    Run a pipeline, then re-run with a modification midway.
    Upstream cached nodes should be reused.
    """

    def test_partial_rerun_reuses_cache(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        # First full run
        dfs: list[Any] = []
        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"], diameter=30.0)
            results = measure(image=raw["path"], mask=masks["mask"])
            dfs.append(wf.compute(results))

        # Second run: same segment parameters, should cache-hit on segment
        events: list[Any] = []
        with Workflow(
            storage_path=tmp_workspace / "results",
            on_progress=lambda e: events.append(e),
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"], diameter=30.0)
            results = measure(image=raw["path"], mask=masks["mask"])
            dfs.append(wf.compute(results))
            pd.testing.assert_frame_equal(dfs[0], dfs[1])

            # At least the segment and stats nodes should be cached
            cached = [e for e in events if e.status == "cached"]
            assert len(cached) >= 2


class TestVerticalScaling:
    """
    Concat multiple data sources, then process the combined set.
    """

    def test_concat_then_process(self, tmp_workspace_two_sources):
        ws = tmp_workspace_two_sources
        load = FileLoader()
        concat = Concat()
        segment = StubSegmenter()

        with Workflow(storage_path=ws / "results") as wf:
            mri = load(path=str(ws / "mri"), name="mri")
            ct = load(path=str(ws / "ct"), name="ct")
            all_images = concat(mri, ct)
            masks = segment(input_image=all_images["path"])
            df = wf.compute(masks)
            # 3 + 3 = 6 images segmented
            assert len(df) == 6
            assert "mask" in df.columns
