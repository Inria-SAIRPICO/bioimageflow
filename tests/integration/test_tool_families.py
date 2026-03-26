"""
Test tool families via inheritance.

Covers:
- Shared environment via base class
- Tag inheritance
- Different tools in same environment
- Unrelated tool reusing an environment without inheritance
"""


from bioimageflow import Workflow

from .conftest import (
    CellposeSegmenter,
    CellposeTrain,
    FileLoader,
    StardistSegmenter,
    StubSegmenter,
    cellpose_env,
    stardist_env,
)


class TestEnvironmentSharing:

    def test_family_shares_environment(self):
        """CellposeSegmenter and CellposeTrain share the cellpose environment."""
        assert CellposeSegmenter.environment is cellpose_env
        assert CellposeTrain.environment is cellpose_env
        assert CellposeSegmenter.environment is CellposeTrain.environment

    def test_different_family_different_environment(self):
        assert StardistSegmenter.environment is stardist_env
        assert StardistSegmenter.environment is not CellposeSegmenter.environment

    def test_unrelated_tool_reuses_environment(self):
        """A tool can reuse an environment without inheriting from the family."""
        assert StubSegmenter.environment is cellpose_env


class TestTagInheritance:

    def test_base_tags_inherited(self):
        assert "cellpose" in CellposeSegmenter.tags

    def test_child_can_extend_tags(self):
        assert "cellpose" in CellposeTrain.tags
        assert "training" in CellposeTrain.tags


class TestFamilyWorkflow:

    def test_cellpose_segment_workflow(self, tmp_workspace):
        load = FileLoader()
        segment = CellposeSegmenter()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"], diameter=25.0)
            df = wf.compute(masks)

            assert len(df) == 3
            assert "mask" in df.columns
            assert "cell_count" in df.columns

    def test_cellpose_train_workflow(self, tmp_workspace):
        load = FileLoader()
        segment = CellposeSegmenter()
        train = CellposeTrain()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            trained = train(
                training_images=raw["path"],
                training_masks=masks["mask"],
                epochs=10,
            )
            df = wf.compute(trained)

            assert len(df) == 3
            assert "model_path" in df.columns

    def test_two_different_environments_in_one_workflow(self, tmp_workspace):
        """Cellpose and Stardist coexist in the same workflow."""
        load = FileLoader()
        cellpose = CellposeSegmenter()
        stardist = StardistSegmenter()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            cp_masks = cellpose(input_image=raw["path"], name="cellpose_seg")
            sd_masks = stardist(input_image=raw["path"], name="stardist_seg")
            out = wf.compute(cp_masks, sd_masks)

            assert len(out["cellpose_seg"]) == 3
            assert len(out["stardist_seg"]) == 3


class TestInnerClassInheritance:

    def test_inputs_not_inherited_by_default(self):
        """Inner classes do NOT automatically inherit from parent's inner classes."""
        cellpose_inputs = set(CellposeSegmenter.Inputs._get_all_annotations().keys())
        train_inputs = set(CellposeTrain.Inputs._get_all_annotations().keys())
        assert cellpose_inputs != train_inputs

    def test_explicit_inner_class_inheritance(self):
        """When explicitly inheriting, parent fields are included."""


        class ExtendedInputs(CellposeSegmenter.Inputs):
            extra_param: float = 1.0

        annotations = ExtendedInputs._get_all_annotations()
        assert "input_image" in annotations  # From parent
        assert "diameter" in annotations  # From parent
        assert "extra_param" in annotations  # From child
