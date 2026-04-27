"""
Test merge DataFrameTools: InnerJoin, CrossJoin, JoinOnColumn, Concat, Collect.

Covers:
- Multi-source workflow with explicit merge
- CrossJoin for combinatorial pairing
- JoinOnColumn with parameterized column and suffixes
- Concat for vertical stacking
- Collect for gathering scattered columns
- Custom merge strategies
"""


import pytest

from bioimageflow import Workflow
from bioimageflow_common_tools import Collect, Concat, CrossJoin, InnerJoin, JoinOnColumn

from .conftest import (
    ColumnRegex,
    CsvLoader,
    FileLoader,
    StubRegistration,
    StubSegmenter,
    StubStats,
)


class TestInnerJoin:

    def test_inner_join_on_index(self, tmp_workspace):
        """InnerJoin merges two DataFrames on their shared index."""
        load = FileLoader()
        segment = StubSegmenter()
        join = InnerJoin()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            # Join raw (path, filename) with masks (mask, cell_count) on index
            merged = join(raw, masks)
            df = wf.compute(merged)

            assert "path" in df.columns
            assert "filename" in df.columns
            assert "mask" in df.columns
            assert "cell_count" in df.columns
            assert len(df) == 3


class TestCrossJoin:

    def test_cross_join_combinatorial(self, tmp_workspace_two_sources):
        ws = tmp_workspace_two_sources
        load = FileLoader()
        cross = CrossJoin()

        with Workflow(storage_path=ws / "results") as wf:
            mri = load(path=str(ws / "mri"), name="mri_loader")
            ct = load(path=str(ws / "ct"), name="ct_loader")
            paired = cross(mri, ct, suffixes=("_mri", "_ct"))
            df = wf.compute(paired)

            # 3 MRI × 3 CT = 9 pairs
            assert len(df) == 9
            assert "path_mri" in df.columns
            assert "path_ct" in df.columns

    def test_cross_join_then_process(self, tmp_workspace_two_sources):
        ws = tmp_workspace_two_sources
        load = FileLoader()
        cross = CrossJoin()
        register = StubRegistration()

        with Workflow(storage_path=ws / "results") as wf:
            mri = load(path=str(ws / "mri"), name="mri_loader")
            ct = load(path=str(ws / "ct"), name="ct_loader")
            paired = cross(mri, ct, suffixes=("_mri", "_ct"))
            registered = register(
                fixed=paired["path_mri"], moving=paired["path_ct"]
            )
            df = wf.compute(registered)

            assert len(df) == 9
            assert "registered" in df.columns


class TestJoinOnColumn:

    def test_join_on_patient_id(self, tmp_workspace_two_sources):
        ws = tmp_workspace_two_sources
        load = FileLoader()
        csv_load = CsvLoader()
        regex = ColumnRegex()
        join = JoinOnColumn()

        with Workflow(storage_path=ws / "results") as wf:
            mri = load(path=str(ws / "mri"), name="mri_loader")
            mri_meta = regex(
                mri,
                column_name="filename",
                regex=r"(?P<patient_id>\w+)_mri",
            )
            patients = csv_load(path=str(ws / "patients.csv"))
            enriched = join(
                mri_meta, patients, join_column="patient_id", how="left"
            )
            df = wf.compute(enriched)

            assert len(df) == 3
            assert "patient_id" in df.columns
            assert "age" in df.columns
            assert "path" in df.columns

    def test_join_two_modalities_on_patient_id(self, tmp_workspace_two_sources):
        """Full multi-source pattern from specs Section 4.1."""
        ws = tmp_workspace_two_sources
        load = FileLoader()
        csv_load = CsvLoader()
        regex = ColumnRegex()
        join = JoinOnColumn()
        register = StubRegistration()

        with Workflow(storage_path=ws / "results") as wf:
            mri = load(path=str(ws / "mri"), name="mri_loader")
            ct = load(path=str(ws / "ct"), name="ct_loader")

            mri_meta = regex(
                mri, column_name="filename", regex=r"(?P<patient_id>\w+)_mri"
            )
            ct_meta = regex(
                ct, column_name="filename", regex=r"(?P<patient_id>\w+)_ct"
            )

            paired = join(
                mri_meta,
                ct_meta,
                join_column="patient_id",
                suffixes=("_mri", "_ct"),
            )

            patients = csv_load(path=str(ws / "patients.csv"))
            enriched = join(paired, patients, join_column="patient_id", how="left")

            registered = register(
                fixed=enriched["path_mri"], moving=enriched["path_ct"]
            )
            df = wf.compute(registered)

            assert len(df) == 3
            assert "registered" in df.columns


class TestConcat:

    def test_concat_vertical_stacking(self, tmp_workspace_two_sources):
        ws = tmp_workspace_two_sources
        load = FileLoader()
        concat = Concat()

        with Workflow(storage_path=ws / "results") as wf:
            mri = load(path=str(ws / "mri"), name="mri_loader")
            ct = load(path=str(ws / "ct"), name="ct_loader")
            combined = concat(mri, ct)
            df = wf.compute(combined)

            # 3 MRI + 3 CT = 6 rows
            assert len(df) == 6
            assert "path" in df.columns


class TestCollect:

    def test_collect_columns_from_multiple_ancestors(self, tmp_workspace):
        """Collect gathers columns from scattered pipeline branches."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()
        collect = Collect()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            stats = measure(image=raw["path"], mask=masks["mask"])
            all_data = collect(raw, masks, stats)
            df = wf.compute(all_data)

            # Collected from all three nodes
            assert "path" in df.columns       # from raw
            assert "mask" in df.columns       # from masks
            assert "mean_intensity" in df.columns  # from stats
            assert len(df) == 3


class TestMergeSchemaPropagation:
    """Node.get_output_schema propagates through merge tools."""

    def test_inner_join_schema(self, tmp_workspace):
        from bioimageflow_common_tools import Files

        with Workflow():
            files = Files()(path=str(tmp_workspace / "data"))
            seg = StubSegmenter()(input_image=files["path"])
            joined = InnerJoin()(files, seg)
            schema = joined.get_output_schema()
            assert schema is not None
            # Files (path, filename) ∪ StubSegmenter (mask, cell_count); first
            # wins on duplicate keys.
            assert set(schema.keys()) >= {"path", "filename", "mask", "cell_count"}

    def test_cross_join_schema_parameter_space_pattern(self):
        """The exact pattern from example-workflows/parameter_space_exploration."""
        from bioimageflow_common_tools import Files, Generate

        with Workflow():
            files = Files()(path="/tmp")
            sens = Generate()(column_name="sensitivity", values=[1, 2])
            size = Generate()(column_name="size", values=[10, 20])
            grid = CrossJoin()(files, sens, size)
            schema = grid.get_output_schema()
            assert schema is not None
            assert set(schema.keys()) == {"path", "filename", "sensitivity", "size"}

    def test_join_on_column_schema(self):
        from bioimageflow_common_tools import Files

        with Workflow():
            mri = Files()(path="/tmp/mri", name="mri")
            ct = Files()(path="/tmp/ct", name="ct")
            joined = JoinOnColumn()(
                mri, ct, join_column="filename", suffixes=("_mri", "_ct"),
            )
            schema = joined.get_output_schema()
            assert schema is not None
            # filename kept once; path appears in both → suffixed.
            assert "filename" in schema
            assert "path_mri" in schema
            assert "path_ct" in schema

    def test_concat_schema(self):
        from bioimageflow_common_tools import Files

        with Workflow():
            mri = Files()(path="/tmp/mri", name="mri")
            ct = Files()(path="/tmp/ct", name="ct")
            stacked = Concat()(mri, ct)
            schema = stacked.get_output_schema()
            assert schema is not None
            assert set(schema.keys()) == {"path", "filename"}

    def test_concat_type_conflict_falls_back_to_any(self):
        """Concat with mismatched column types yields the 'any' fallback."""
        a = {"shared": {"type": "int", "default": None, "image_spec": None}}
        b = {"shared": {"type": "str", "default": None, "image_spec": None}}
        merged = Concat.resolve_merge_schema([a, b])
        assert merged is not None
        assert merged["shared"]["type"] == "any"
        assert merged["shared"]["image_spec"] is None

    def test_concat_same_type_keeps_first(self):
        """Concat with matching column types keeps the first entry verbatim."""
        a = {"shared": {"type": "int", "default": 1, "image_spec": None}}
        b = {"shared": {"type": "int", "default": 2, "image_spec": None}}
        merged = Concat.resolve_merge_schema([a, b])
        assert merged is not None
        assert merged["shared"] == a["shared"]

    def test_collect_schema_renames_duplicates(self):
        from bioimageflow_common_tools import Files

        with Workflow():
            a = Files()(path="/tmp/a", name="a")
            b = Files()(path="/tmp/b", name="b")
            collected = Collect()(a, b)
            schema = collected.get_output_schema()
            assert schema is not None
            # Both have path/filename → second's get _1 suffix.
            assert "path" in schema
            assert "path_1" in schema
            assert "filename" in schema
            assert "filename_1" in schema

    def test_merge_unresolvable_when_upstream_unresolvable(self):
        """If any upstream returns None, merge propagates None."""
        from bioimageflow.dataframe_tool import DataFrameTool
        from bioimageflow_common_tools import Files
        from bioimageflow_core import IOModel

        class Unknown(DataFrameTool):
            display_name = "Unknown"

            class Inputs(IOModel):
                pass

            @classmethod
            def resolve_outputs(cls, inputs=None):
                return None  # always unresolvable

        with Workflow():
            files = Files()(path="/tmp")
            unk = Unknown()()
            joined = InnerJoin()(files, unk)
            assert joined.get_output_schema() is None

    def test_construction_time_columnref_after_merge(self):
        """node['col'] validates against the merged schema."""
        from bioimageflow_common_tools import Files, Generate

        with Workflow():
            files = Files()(path="/tmp")
            sens = Generate()(column_name="sensitivity", values=[1, 2])
            grid = CrossJoin()(files, sens)
            # Existing column → OK.
            ref = grid["sensitivity"]
            assert ref.column == "sensitivity"
            # Missing column → raises.
            from bioimageflow import ColumnNotFoundError
            with pytest.raises(ColumnNotFoundError):
                _ = grid["nonexistent"]
