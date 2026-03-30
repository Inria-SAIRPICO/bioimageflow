"""
Test error handling and validation.

Covers:
- BindingError: missing required input field
- ColumnNotFoundError: bad column reference (with close-match suggestions)
- IndexAlignmentError: incompatible upstream indices
- Cycle detection at graph construction time
- Type incompatibility warning/error at construction time
- Template errors: undefined variables
- Worker exceptions: re-raised with original stack trace
- IOModel validation: unknown/missing fields
- ProcessingTool must implement process_row or process_batch
"""

import pytest

from bioimageflow import CrossJoin, Workflow
from typing import Annotated
from pathlib import Path

from bioimageflow_core import (
    Arguments,
    IOModel,
    ImageSpec,
    ProcessingTool,
    Semantic,
)

from .conftest import FileLoader, StubSegmenter, StubStats, imageio_env


class TestBindingError:

    def test_missing_required_input_raises(self, tmp_workspace):
        """ProcessingTool called without a required field raises BindingError."""
        load = FileLoader()
        segment = StubSegmenter()

        with pytest.raises(Exception, match="input_image|missing|required|Binding"):
            with Workflow(storage_path=tmp_workspace / "results"):
                _raw = load(path=str(tmp_workspace / "data"))
                # Missing required 'input_image' argument
                segment(diameter=30.0)

    def test_extra_unknown_kwarg_raises(self, tmp_workspace):
        """Unknown keyword argument raises an error."""
        load = FileLoader()
        segment = StubSegmenter()

        with pytest.raises(Exception, match="unknown|unexpected|extra"):
            with Workflow(storage_path=tmp_workspace / "results"):
                raw = load(path=str(tmp_workspace / "data"))
                segment(input_image=raw["path"], nonexistent_param=42)


class TestColumnNotFoundError:

    def test_bad_column_reference_raises(self, tmp_workspace):
        """Referencing a non-existent column raises ColumnNotFoundError."""
        load = FileLoader()
        segment = StubSegmenter()

        with pytest.raises(Exception, match="column|not found|ColumnNotFound"):
            with Workflow(storage_path=tmp_workspace / "results"):
                raw = load(path=str(tmp_workspace / "data"))
                segment(input_image=raw["nonexistent_column"])

    def test_close_match_suggestion(self, tmp_workspace):
        """Error message suggests close matches for typos."""
        load = FileLoader()
        segment = StubSegmenter()

        with pytest.raises(Exception, match="(?i)path|did you mean"):
            with Workflow(storage_path=tmp_workspace / "results"):
                raw = load(path=str(tmp_workspace / "data"))
                # "pat" is close to "path"
                segment(input_image=raw["pat"])

    def test_node_shorthand_column_not_found(self, tmp_workspace):
        """Node shorthand: field=node fails if upstream has no column named 'field'."""
        load = FileLoader()
        measure = StubStats()

        with pytest.raises(Exception, match="image|column|not found"):
            with Workflow(storage_path=tmp_workspace / "results"):
                raw = load(path=str(tmp_workspace / "data"))
                # StubStats.Inputs.image → raw["image"], but raw has "path", not "image"
                measure(image=raw, mask=raw)


class TestIndexAlignmentError:

    def test_incompatible_indices_raise(self, tmp_workspace):
        """Two independent sources with no common lineage raise IndexAlignmentError."""
        load = FileLoader()
        segment = StubSegmenter()

        with pytest.raises(
            Exception, match="index|alignment|lineage|IndexAlignment"
        ):
            with Workflow(storage_path=tmp_workspace / "results") as wf:
                source_a = load(path=str(tmp_workspace / "data"), name="src_a")
                source_b = load(path=str(tmp_workspace / "data"), name="src_b")
                # Referencing columns from two independent sources without merge
                segment(input_image=source_a["path"])
                # This next line references both unrelated sources
                measure = StubStats()
                result = measure(
                    image=source_a["path"], mask=source_b["path"]
                )
                wf.compute(result)

    def test_merge_resolves_alignment(self, tmp_workspace):
        """Using CrossJoin resolves the index alignment issue."""
        load = FileLoader()
        cross = CrossJoin()
        measure = StubStats()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            source_a = load(path=str(tmp_workspace / "data"), name="src_a")
            source_b = load(path=str(tmp_workspace / "data"), name="src_b")
            paired = cross(source_a, source_b, suffixes=("_a", "_b"))
            result = measure(
                image=paired["path_a"], mask=paired["path_b"]
            )
            df = wf.compute(result)

            # 3 × 3 = 9 combinations
            assert len(df) == 9


class TestCycleDetection:

    def test_cycle_raises_at_compute(self, tmp_workspace):
        """Manually injecting a cycle into the DAG raises an error at compute time."""
        load = FileLoader()
        segment = StubSegmenter()

        with pytest.raises(Exception, match="cycle|circular|DAG|acyclic"):
            with Workflow(storage_path=tmp_workspace / "results") as wf:
                raw = load(path=str(tmp_workspace / "data"))
                masks = segment(input_image=raw["path"])
                # Manually inject a back-edge to create a cycle
                # (bypasses the normal API which prevents this by construction)
                raw._upstream_nodes = getattr(raw, "_upstream_nodes", set()) | {masks}
                wf.compute(masks)

    def test_forward_pipeline_has_no_cycle(self, tmp_workspace):
        """A normal forward pipeline should not trigger cycle detection."""
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            df = wf.compute(masks)

            assert len(df) == 3


class TestTypeIncompatibility:

    def test_type_mismatch_raises_at_construction(self, tmp_workspace):
        """Feeding LABEL output to a tool expecting DISPLACEMENT raises."""

        class DisplacementConsumer(ProcessingTool):
            display_name = "Disp Consumer"
            environment = imageio_env

            class Inputs(IOModel):
                field: Annotated[Path, ImageSpec(semantics={Semantic.DISPLACEMENT})]

            class Outputs(IOModel):
                result: float

            def process_row(self, arguments):
                return self.Outputs(result=0.0)

        load = FileLoader()
        segment = StubSegmenter()
        consumer = DisplacementConsumer()

        with pytest.raises(Exception, match="compatible|type|semantic|mismatch"):
            with Workflow(storage_path=tmp_workspace / "results"):
                raw = load(path=str(tmp_workspace / "data"))
                masks = segment(input_image=raw["path"])
                # mask is LABEL, consumer wants DISPLACEMENT → incompatible
                consumer(field=masks["mask"])


class TestWorkerExceptions:

    def test_worker_exception_reraise(self, tmp_workspace):
        """Exceptions in process_row are re-raised in the main process."""

        class FailingTool(ProcessingTool):
            display_name = "Failing Tool"
            environment = imageio_env

            class Inputs(IOModel):
                input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

            class Outputs(IOModel):
                result: float

            def process_row(self, arguments):
                raise ValueError("Intentional failure in worker")

        load = FileLoader()
        tool = FailingTool()

        with pytest.raises(ValueError, match="Intentional failure"):
            with Workflow(storage_path=tmp_workspace / "results") as wf:
                raw = load(path=str(tmp_workspace / "data"))
                output = tool(input_image=raw["path"])
                wf.compute(output)


class TestIndexLineageHelpers:

    def test_parse_index_lineage_simple(self):
        from bioimageflow_core.arguments import parse_index_lineage
        assert parse_index_lineage("img_001") == ["img_001"]

    def test_parse_index_lineage_exploded(self):
        from bioimageflow_core.arguments import parse_index_lineage
        assert parse_index_lineage("img_001::0::2") == ["img_001", "0", "2"]

    def test_parent_index_simple(self):
        from bioimageflow_core.arguments import parent_index
        assert parent_index("img_001") == "img_001"

    def test_parent_index_exploded(self):
        from bioimageflow_core.arguments import parent_index
        assert parent_index("img_001::0::2") == "img_001::0"

    def test_parent_index_single_explosion(self):
        from bioimageflow_core.arguments import parent_index
        assert parent_index("img_001::0") == "img_001"


class TestIOModelValidation:

    def test_unknown_fields_raise(self):
        class MyModel(IOModel):
            x: int
            y: float = 1.0

        with pytest.raises(TypeError, match="Unknown fields"):
            MyModel(x=1, y=2.0, z=3)

    def test_missing_required_field_raise(self):
        class MyModel(IOModel):
            x: int
            y: float = 1.0

        with pytest.raises(TypeError, match="Missing required"):
            MyModel(y=2.0)

    def test_valid_construction(self):
        class MyModel(IOModel):
            x: int
            y: float = 1.0

        m = MyModel(x=42)
        assert m.x == 42
        assert m.y == 1.0

    def test_repr(self):
        class MyModel(IOModel):
            x: int

        m = MyModel(x=5)
        assert "MyModel" in repr(m)
        assert "5" in repr(m)


class TestProcessingToolValidation:

    def test_must_implement_process_method(self):
        """ProcessingTool subclass without process_row or process_batch raises TypeError."""
        with pytest.raises(TypeError, match="process_row|process_batch"):

            class EmptyTool(ProcessingTool):
                display_name = "Empty"
                environment = imageio_env

                class Inputs(IOModel):
                    x: int

                class Outputs(IOModel):
                    y: int

                # Neither process_row nor process_batch implemented


class TestDeferredColumnValidation:

    def test_missing_column_in_dynamic_upstream_fails_at_execution(self, tmp_workspace):
        """Column ref to a DataFrameTool without Outputs is deferred; fails at compute."""
        _load = FileLoader()
        segment = StubSegmenter()

        # CsvLoader has no Outputs declaration — column validation is deferred
        from .conftest import CsvLoader
        csv_load = CsvLoader()

        with pytest.raises(Exception, match="column|not found|ColumnNotFound|KeyError"):
            with Workflow(storage_path=tmp_workspace / "results") as wf:
                data = csv_load(path=str(tmp_workspace / "data" / "cell_01.tif"))
                # 'nonexistent' won't be caught at construction (no Outputs)
                segment(input_image=data["nonexistent"])
                # Error surfaces at compute time
                wf.compute()


class TestArgumentsTypoSuggestion:

    def test_typo_suggests_close_matches(self):
        args = Arguments(input_image="/path/to/img.tif", diameter=30.0)
        with pytest.raises(AttributeError, match="Did you mean.*input_image"):
            _ = args.input_imag  # Typo

    def test_no_close_match_lists_available(self):
        args = Arguments(x=1, y=2)
        with pytest.raises(AttributeError, match="Available fields"):
            _ = args.completely_wrong_name
