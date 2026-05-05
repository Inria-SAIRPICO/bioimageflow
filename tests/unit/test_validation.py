"""Unit tests for bioimageflow.validation."""

from pathlib import Path
from typing import Annotated

from bioimageflow_core.types import (
    Connectable,
    GUIMeta,
    ImagePath,
    ImageSpec,
    Semantic,
    SharedArray,
    extract_gui_meta,
)
from bioimageflow_core.tool import IOModel, ProcessingTool
from bioimageflow_core.environment import EnvironmentSpec
from bioimageflow.validation import (
    is_path_type,
    is_image_type,
    extract_image_spec,
    get_inputs_schema,
    get_source_hash,
)


class TestIsPathType:

    def test_plain_path(self):
        assert is_path_type(Path) is True

    def test_annotated_path(self):
        ann = Annotated[Path, ImageSpec()]
        assert is_path_type(ann) is True

    def test_non_path(self):
        assert is_path_type(int) is False
        assert is_path_type(str) is False

    def test_annotated_non_path(self):
        ann = Annotated[str, "metadata"]
        assert is_path_type(ann) is False


class TestIsImageType:

    def test_annotated_with_image_spec(self):
        ann = Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]
        assert is_image_type(ann) is True

    def test_plain_path_is_not_image(self):
        assert is_image_type(Path) is False

    def test_shared_array_with_spec(self):
        ann = Annotated[SharedArray, ImageSpec(semantics={Semantic.INTENSITY})]
        assert is_image_type(ann) is True


class TestExtractImageSpec:

    def test_returns_spec(self):
        spec = ImageSpec(semantics={Semantic.LABEL})
        ann = Annotated[Path, spec]
        assert extract_image_spec(ann) is spec

    def test_returns_none_for_plain(self):
        assert extract_image_spec(int) is None

    def test_image_path_gui_meta_preserves_image_spec(self):
        ann = ImagePath(
            semantics=Semantic.INTENSITY,
            gui=GUIMeta(connectable=Connectable.BY_DEFAULT),
        )
        spec = extract_image_spec(ann)
        assert spec is not None
        assert spec.semantics == {Semantic.INTENSITY}


class TestExtractGUIMeta:

    def test_image_path_gui_meta_preserved(self):
        gui = GUIMeta(display_name="Input image", connectable=Connectable.BY_DEFAULT)
        ann = ImagePath(semantics=Semantic.INTENSITY, gui=gui)
        assert extract_gui_meta(ann) is gui


class TestGetInputsSchema:

    def test_basic_schema(self):
        class Tool(ProcessingTool):
            display_name = "Test Tool"
            environment = EnvironmentSpec(name="test", dependencies={})

            class Inputs(IOModel):
                image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
                sigma: float = 1.0

            class Outputs(IOModel):
                result: float

            def process_row(self, arguments):
                return self.Outputs(result=0.0)

        schema = get_inputs_schema(Tool())
        assert "image" in schema
        assert "sigma" in schema
        assert schema["image"]["required"] is True
        assert schema["image"]["connectable"] is Connectable.NOT_BY_DEFAULT
        assert schema["image"]["type"] is Path
        assert schema["image"]["image_spec"] is not None
        assert schema["sigma"]["required"] is False
        assert schema["sigma"]["default"] == 1.0
        assert schema["sigma"]["connectable"] is Connectable.NOT_BY_DEFAULT

    def test_gui_meta_numeric_constraints(self):
        class Tool(ProcessingTool):
            display_name = "Test Tool Meta"
            environment = EnvironmentSpec(name="test", dependencies={})

            class Inputs(IOModel):
                diameter: Annotated[float, GUIMeta(connectable=Connectable.NEVER, min=1.0, max=500.0, step=0.5)] = 30.0

            class Outputs(IOModel):
                result: float

            def process_row(self, arguments):
                return self.Outputs(result=0.0)

        schema = get_inputs_schema(Tool())
        d = schema["diameter"]
        assert d["connectable"] is Connectable.NEVER
        assert d["min"] == 1.0
        assert d["max"] == 500.0
        assert d["step"] == 0.5
        assert d["default"] == 30.0
        assert d["type"] is float

    def test_gui_meta_coexists_with_image_spec(self):
        class Tool(ProcessingTool):
            display_name = "Test Tool Both"
            environment = EnvironmentSpec(name="test", dependencies={})

            class Inputs(IOModel):
                image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY}), GUIMeta(connectable=Connectable.BY_DEFAULT)]

            class Outputs(IOModel):
                result: float

            def process_row(self, arguments):
                return self.Outputs(result=0.0)

        schema = get_inputs_schema(Tool())
        assert schema["image"]["connectable"] is Connectable.BY_DEFAULT
        assert schema["image"]["image_spec"] is not None
        assert schema["image"]["type"] is Path

    def test_no_gui_meta_defaults_connectable(self):
        class Tool(ProcessingTool):
            display_name = "Test Tool No Meta"
            environment = EnvironmentSpec(name="test", dependencies={})

            class Inputs(IOModel):
                threshold: float = 0.5

            class Outputs(IOModel):
                result: float

            def process_row(self, arguments):
                return self.Outputs(result=0.0)

        schema = get_inputs_schema(Tool())
        assert schema["threshold"]["connectable"] is Connectable.NOT_BY_DEFAULT
        assert "min" not in schema["threshold"]
        assert "max" not in schema["threshold"]
        assert "step" not in schema["threshold"]
        assert "group" not in schema["threshold"]

    def test_gui_meta_group(self):
        class Tool(ProcessingTool):
            display_name = "Test Tool Group"
            environment = EnvironmentSpec(name="test", dependencies={})

            class Inputs(IOModel):
                sigma: Annotated[float, GUIMeta(group="general")] = 1.0
                use_gpu: Annotated[bool, GUIMeta(connectable=Connectable.NEVER, group="gpu")] = False
                iterations: Annotated[int, GUIMeta(min=1, max=100, group="advanced")] = 10

            class Outputs(IOModel):
                result: float

            def process_row(self, arguments):
                return self.Outputs(result=0.0)

        schema = get_inputs_schema(Tool())
        assert schema["sigma"]["group"] == "general"
        assert schema["use_gpu"]["group"] == "gpu"
        assert schema["use_gpu"]["connectable"] is Connectable.NEVER
        assert schema["iterations"]["group"] == "advanced"
        assert schema["iterations"]["min"] == 1

    def test_partial_numeric_constraints(self):
        class Tool(ProcessingTool):
            display_name = "Test Tool Partial"
            environment = EnvironmentSpec(name="test", dependencies={})

            class Inputs(IOModel):
                count: Annotated[int, GUIMeta(min=0)] = 5

            class Outputs(IOModel):
                result: float

            def process_row(self, arguments):
                return self.Outputs(result=0.0)

        schema = get_inputs_schema(Tool())
        assert schema["count"]["min"] == 0
        assert "max" not in schema["count"]
        assert "step" not in schema["count"]


class TestGetSourceHash:

    def test_produces_hex_string(self):
        h = get_source_hash(TestGetSourceHash)
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex

    def test_deterministic(self):
        assert get_source_hash(TestGetSourceHash) == get_source_hash(TestGetSourceHash)
