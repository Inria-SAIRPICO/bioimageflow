"""Unit tests for bioimageflow.template."""

from pathlib import Path

import pytest

from bioimageflow_core import Template
from bioimageflow_core.tool import IOModel
from bioimageflow.template import get_output_templates, resolve_template, validate_template


class TestResolveTemplate:

    def test_simple_variables(self):
        result = resolve_template(
            "{node_name}_{row_index}",
            {"node_name": "seg", "row_index": "0"},
        )
        assert result == "seg_0"

    def test_field_stem(self):
        result = resolve_template(
            "{img.stem}_out.tif",
            {"img": "/data/cell_01.tif"},
        )
        assert result == "cell_01_out.tif"

    def test_field_ext(self):
        result = resolve_template(
            "output{img.ext}",
            {"img": "/data/photo.png"},
        )
        assert result == "output.png"

    def test_field_exts(self):
        result = resolve_template(
            "output{img.exts}",
            {"img": "/data/stack.ome.tif"},
        )
        assert result == "output.ome.tif"

    def test_bare_input_field(self):
        result = resolve_template(
            "{img.stem}_ch{channel}{img.ext}",
            {"img": "/data/photo.tif", "channel": 2},
        )
        assert result == "photo_ch2.tif"

    def test_ext_special_variable(self):
        result = resolve_template(
            "{node_name}_{row_index}{ext}",
            {"node_name": "seg", "row_index": "0", "_ext": ".png"},
        )
        assert result == "seg_0.png"

    def test_column_reference(self):
        result = resolve_template(
            "{column:patient}_mask.png",
            {"_columns": {"patient": "A001"}},
        )
        assert result == "A001_mask.png"

    def test_unknown_column_left_as_is(self):
        result = resolve_template(
            "{column:missing}",
            {"_columns": {}},
        )
        assert result == "{column:missing}"


class TestValidateTemplate:

    def test_valid_field_ref(self):
        validate_template("{img.stem}_out.tif", {"img": Path})

    def test_invalid_field_ref_raises(self):
        with pytest.raises(ValueError, match="undefined input field"):
            validate_template("{nonexistent.stem}_out.tif", {"img": Path})

    def test_special_vars_not_flagged(self):
        validate_template("{node_name}_{row_index}{ext}", {"img": Path})


class TestGetOutputTemplates:

    def test_explicit_template_marker(self):
        class Inp(IOModel):
            img: Path

        class Out(IOModel):
            result: Path = Template("{img.stem}_out.tif")

        templates = get_output_templates(Out, Inp)
        assert templates["result"] == "{img.stem}_out.tif"

    def test_template_default_must_be_path_output(self):
        class Inp(IOModel):
            img: Path

        class Out(IOModel):
            status: str = Template("{img.stem}_status.txt")  # type: ignore[assignment]

        with pytest.raises(TypeError, match="Template default.*path output"):
            get_output_templates(Out, Inp)

    def test_string_template_default_raises(self):
        class Inp(IOModel):
            img: Path

        class Out(IOModel):
            result: Path = "{img.stem}_out.tif"  # type: ignore[assignment]

        with pytest.raises(TypeError, match="must be declared with Template"):
            get_output_templates(Out, Inp)

    def test_path_template_default_raises(self):
        class Inp(IOModel):
            img: Path

        class Out(IOModel):
            result: Path = Path("{img.stem}_out.tif")

        with pytest.raises(TypeError, match="must be declared with Template"):
            get_output_templates(Out, Inp)

    def test_static_string_default_raises(self):
        class Inp(IOModel):
            img: Path

        class Out(IOModel):
            result: Path = "fixed.tif"  # type: ignore[assignment]

        with pytest.raises(TypeError, match="must be declared with Template"):
            get_output_templates(Out, Inp)

    def test_static_path_default_raises(self):
        class Inp(IOModel):
            img: Path

        class Out(IOModel):
            result: Path = Path("fixed.tif")

        with pytest.raises(TypeError, match="must be declared with Template"):
            get_output_templates(Out, Inp)

    def test_static_template_default_is_valid(self):
        class Inp(IOModel):
            img: Path

        class Out(IOModel):
            result: Path = Template("fixed.tif")

        templates = get_output_templates(Out, Inp)
        assert templates["result"] == "fixed.tif"

    def test_default_template_single_path_input(self):
        class Inp(IOModel):
            img: Path

        class Out(IOModel):
            result: Path

        templates = get_output_templates(Out, Inp)
        assert "{ext}" in templates["result"]

    def test_non_path_fields_skipped(self):
        class Inp(IOModel):
            x: int

        class Out(IOModel):
            count: int

        templates = get_output_templates(Out, Inp)
        assert "count" not in templates

@pytest.mark.parametrize("consumption", ["mapped", "collective"])
def test_input_path_echo_preserves_source_and_generates_declared_destinations(tmp_path, consumption):
    from bioimageflow import Workflow
    from bioimageflow_core import GENERAL_ENV, ProcessingTool, RowConsumption

    source = tmp_path / "source.txt"
    source.write_text("original sentinel")
    supplied = tmp_path / "supplied.txt"
    supplied.write_text("supplied sentinel")

    class Echo(ProcessingTool):
        accepts_upstream = False
        environment = GENERAL_ENV
        row_consumption = RowConsumption(consumption)

        class Inputs(IOModel):
            source: Path
            explicit: Path

        class Outputs(IOModel):
            source: Path
            explicit: Path = Template("explicit.txt")
            generated: Path

        def process(self, arguments, *, context=None):
            assert Path(arguments.source) == source
            assert Path(arguments.source).read_text() == "original sentinel"
            assert Path(arguments.explicit) != supplied
            assert Path(arguments.generated) not in (source, supplied)
            Path(arguments.explicit).write_text("explicit destination")
            Path(arguments.generated).write_text("generated destination")
            return self.Outputs(source=arguments.source, explicit=arguments.explicit,
                                generated=arguments.generated)

        def process_batch(self, arguments_list, *, context=None):
            if consumption == "mapped":
                return [self.process(arguments, context=context) for arguments in arguments_list]
            assert arguments_list == []
            return [self.process(context.batch_arguments, context=context)]

    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        node = Echo()(source=source, explicit=supplied)
        result = workflow.compute(node)
    assert [Path(value) for value in result["source"]] == [source]
    assert Path(result.iloc[0]["explicit"]).read_text() == "explicit destination"
    assert Path(result.iloc[0]["generated"]).read_text() == "generated destination"
    assert source.read_text() == "original sentinel"
    assert supplied.read_text() == "supplied sentinel"


def test_nullable_path_echo_keeps_default_and_allows_explicit_override():
    class Inp(IOModel):
        source: Path | None = None

    class Out(IOModel):
        source: Path | None
        generated: Path

    assert get_output_templates(Out, Inp) == {"generated": "{node_name}_{row_index}{ext}"}
    assert get_output_templates(Out, Inp, {"source": "copied.txt"})["source"] == "copied.txt"


def test_scalar_input_name_does_not_suppress_path_destination():
    class Inp(IOModel):
        result: str

    class Out(IOModel):
        result: Path

    assert "result" in get_output_templates(Out, Inp)
