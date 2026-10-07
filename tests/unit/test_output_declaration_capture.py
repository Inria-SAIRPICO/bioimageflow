"""Nominal result admission is separate from captured output declaration values."""

from copy import deepcopy
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from bioimageflow import Workflow
from bioimageflow.engine.output_validation import (
    normalize_processing_batch_outputs,
    validate_processing_output,
)
from bioimageflow.validation import serialize_output_schema
from bioimageflow.workflow.capture import capture_output_declaration
from bioimageflow_core import EnvironmentSpec, IOModel, Template
from tests.testkit.output_alias_tools import AliasOutputs, GlobalAliasRow, UnrelatedAliasOutputs


def _captured_node(tmp_path):
    with Workflow(engine="direct", storage_path=tmp_path):
        source = GlobalAliasRow()()
    return deepcopy(source)


def test_nominal_class_changes_cannot_replace_captured_schema(tmp_path, monkeypatch):
    captured = _captured_node(tmp_path)
    monkeypatch.setattr(AliasOutputs, "__annotations__", {"result": Path, "value": str})
    monkeypatch.setattr(AliasOutputs, "value", "later-default", raising=False)
    monkeypatch.setattr(AliasOutputs, "result", Template("later_{row_index}.txt"))
    repeated = deepcopy(captured)
    frozen = repeated.tool.Outputs
    declaration = repeated._captured_output_declaration

    schema = serialize_output_schema(repeated.tool)
    assert list(frozen._get_all_annotations()) == ["value", "result"]
    assert schema["value"]["required"] is True
    assert schema["result"]["template"] == "alias_{row_index}.txt"
    accepted = validate_processing_output(
        AliasOutputs(value=7, result=tmp_path / "value.txt"), frozen,
        nominal_output_type=declaration.nominal_type,
    )
    assert accepted.value == 7
    with pytest.raises(TypeError, match="field 'value'"):
        validate_processing_output(
            AliasOutputs(value="bad", result=tmp_path / "value.txt"), frozen,
            nominal_output_type=declaration.nominal_type,
        )


def test_explicit_declaration_replacement_resets_output_class_authority(tmp_path):
    captured = _captured_node(tmp_path)
    captured.tool.Outputs = UnrelatedAliasOutputs
    replaced = deepcopy(captured)
    declaration = replaced._captured_output_declaration
    destination = tmp_path / "value.txt"

    accepted = validate_processing_output(
        UnrelatedAliasOutputs(value=7, result=destination), replaced.tool.Outputs,
        nominal_output_type=declaration.nominal_type,
    )
    assert accepted.value == 7
    with pytest.raises(TypeError, match="plain dictionary"):
        validate_processing_output(
            AliasOutputs(value=7, result=destination), replaced.tool.Outputs,
            nominal_output_type=declaration.nominal_type,
        )


@pytest.mark.parametrize("mode", ["mapped-flat", "mapped-nested", "collective"])
def test_batch_output_forms_keep_the_admitted_nominal_class(tmp_path, mode):
    captured = _captured_node(tmp_path)
    output = AliasOutputs(value=7, result=tmp_path / "value.txt")
    raw = [[output], []] if mode == "mapped-nested" else [output]
    accepted = normalize_processing_batch_outputs(
        raw, captured.tool.Outputs,
        expected_rows=2 if mode == "mapped-nested" else 1,
        row_consumption="collective" if mode == "collective" else "mapped",
        nominal_output_type=captured._captured_output_declaration.nominal_type,
    )
    assert [[item.value for item in group] for group in accepted] == (
        [[7], []] if mode == "mapped-nested" else [[7]]
    )


_MISSING = object()
_PADDED = np.dtype({"names": ["value"], "formats": ["int32"], "offsets": [0], "itemsize": 8})


@pytest.mark.parametrize("initial,replacement", [
    (_MISSING, None), (None, _MISSING), (True, 1), (-0.0, 0.0),
    (Template("first.txt"), Template("second.txt")),
    ({"values": [1, 2]}, {"values": [1, 3]}),
    (np.array([1, 2], dtype="int32"), np.array([1, 3], dtype="int32")),
    (np.array([(1,), (2,)], dtype=_PADDED), np.array([(1,), (3,)], dtype=_PADDED)),
], ids=["missing-none", "none-missing", "bool-int", "signed-zero", "template", "nested", "array", "structured-array"])
def test_nominal_default_drift_preserves_typed_value_meaning(initial, replacement):
    annotation = Path if isinstance(initial, Template) else Any
    model = type("Defaults", (IOModel,), {
        "__module__": __name__, "__annotations__": {"value": annotation},
    })
    if initial is not _MISSING:
        model.value = initial
    captured = capture_output_declaration(model)
    captured.require_current_nominal(captured.frozen_model)
    if replacement is _MISSING:
        del model.value
    else:
        model.value = replacement

    with pytest.raises(ValueError, match="Nominal output declaration changed"):
        captured.require_current_nominal(captured.frozen_model)


def test_unchanged_supported_defaults_keep_finite_capture_authority():
    class Choice(Enum):
        FIRST = "first"

    defaults = {
        "bytes": b"pixels", "complex": 1 + 2j, "path": Path("relative.txt"),
        "enum": Choice.FIRST, "set": {1, 2}, "frozen": frozenset({"x"}),
        "tuple": (False, None, np.int32(3)), "array": np.array([1.0, -0.0]),
        "structured": np.array([(3,), (7,)], dtype=_PADDED),
        "structured-scalar": np.array([(3,)], dtype=_PADDED)[0],
        "template": Template("owned_{row_index}.txt"),
        "recipe": EnvironmentSpec(name="default-recipe", dependencies={}),
    }
    model = type("Defaults", (IOModel,), {
        "__module__": __name__, "__annotations__": {"value": Any}, "value": defaults,
    })
    first = capture_output_declaration(model)
    repeated = capture_output_declaration(first.frozen_model, first)

    repeated.require_current_nominal(repeated.frozen_model)
