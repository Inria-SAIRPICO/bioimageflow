"""
Test 1-to-N output explosion (tiling, splitting, etc.).

Covers:
- process_row returning list[Outputs]
- Index explosion with :: separator
- Nested explosions (successive 1-to-N)
- Index alignment between exploded and non-exploded upstream
- Downstream processing of exploded rows
"""


from bioimageflow import Workflow
from bioimageflow_common_tools import Collect

from .conftest import FileLoader, StubSegmenter, StubTiler


class TestBasicExplosion:

    def test_tiler_produces_multiple_outputs_per_row(self, tmp_workspace):
        load = FileLoader()
        tile = StubTiler()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            tiles = tile(input_image=raw["path"], tile_count=4)
            df = wf.compute(tiles)

            # 3 images × 4 tiles = 12 rows
            assert len(df) == 12
            assert "tile" in df.columns

    def test_explosion_index_uses_separator(self, tmp_workspace):
        """Exploded indices use :: as separator."""
        load = FileLoader()
        tile = StubTiler()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            tiles = tile(input_image=raw["path"], tile_count=2)
            df = wf.compute(tiles)

            # Each original index gets ::0 and ::1 children
            for idx in df.index:
                assert "::" in str(idx)

    def test_single_output_preserves_parent_index(self, tmp_workspace):
        """When process_row returns a single Outputs, index is unchanged."""
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            df = wf.compute(masks)

            # No explosion — original indices preserved
            for idx in df.index:
                assert "::" not in str(idx)


class TestExplosionAlignment:

    def test_align_exploded_with_coarser_upstream(self, tmp_workspace):
        """
        When tiles (exploded) and raw (original) are both referenced,
        the engine expands raw to match tile indices via parent lookup.
        """
        load = FileLoader()
        tile = StubTiler()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            tiles = tile(input_image=raw["path"], tile_count=2)
            # The key test: Collect(raw, tiles) aligns raw's coarser index
            # to tiles' finer exploded index via parent-index expansion
            collect = Collect()
            all_data = collect(raw, tiles)
            df = wf.compute(all_data)

            # 3 images × 2 tiles = 6 rows, with raw columns expanded
            assert len(df) == 6
            assert "path" in df.columns
            assert "tile" in df.columns


class TestNestedExplosion:

    def test_successive_explosions_nest_indices(self, tmp_workspace):
        """Tile → Tile again produces nested :: indices."""
        load = FileLoader()
        tile = StubTiler()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            first_tiles = tile(input_image=raw["path"], tile_count=2, name="tile1")
            second_tiles = tile(input_image=first_tiles["tile"], tile_count=2, name="tile2")
            df = wf.compute(second_tiles)

            # 3 × 2 × 2 = 12 rows
            assert len(df) == 12
            # Indices should have two levels of ::
            for idx in df.index:
                parts = str(idx).split("::")
                assert len(parts) == 3  # original::first::second


class TestDownstreamOfExplosion:

    def test_process_each_tile(self, tmp_workspace):
        """Segmenter processes each tile individually after explosion."""
        load = FileLoader()
        tile = StubTiler()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            tiles = tile(input_image=raw["path"], tile_count=3)
            masks = segment(input_image=tiles["tile"])
            df = wf.compute(masks)

            # 3 images × 3 tiles = 9 segmentations
            assert len(df) == 9
            assert "mask" in df.columns
            assert "cell_count" in df.columns
