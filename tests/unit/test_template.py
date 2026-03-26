"""Unit tests for bioimageflow.template."""

import pytest

from bioimageflow_core.tool import IOModel
from bioimageflow.template import resolve_template, validate_template, get_output_templates
from pathlib import Path


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

    def test_explicit_template(self):
        class Inp(IOModel):
            img: Path

        class Out(IOModel):
            result: Path = "{img.stem}_out.tif"  # type: ignore[assignment]

        templates = get_output_templates(Out, Inp)
        assert templates["result"] == "{img.stem}_out.tif"

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
