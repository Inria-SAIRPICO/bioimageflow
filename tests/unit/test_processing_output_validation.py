"""Canonical ProcessingTool output validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from bioimageflow.engine.output_validation import (
    normalize_processing_batch_outputs,
    normalize_processing_row_outputs,
    validate_processing_output,
    validate_processing_result_rows,
)
from bioimageflow_core import IOModel
from bioimageflow_core.worker_protocol import RowResultV1


class Outputs(IOModel):
    path: Path
    count: int
    label: str


def test_plain_worker_output_is_validated_and_restores_declared_values() -> None:
    output = validate_processing_output(
        {"label": "cell", "path": "/shared/mask.tif", "count": 2},
        Outputs,
    )

    assert isinstance(output, Outputs)
    assert output.path == Path("/shared/mask.tif")
    assert output.count == 2
    assert output.label == "cell"


@pytest.mark.parametrize(
    "payload",
    [
        {"path": "/shared/mask.tif", "count": 2},
        {
            "path": "/shared/mask.tif",
            "count": 2,
            "label": "cell",
            "extra": True,
        },
    ],
)
def test_output_fields_must_match_exactly(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="fields"):
        validate_processing_output(payload, Outputs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("count", True),
        ("count", "2"),
        ("label", 2),
    ],
)
def test_output_values_use_strict_annotation_validation(
    field: str,
    value: object,
) -> None:
    payload: dict[str, object] = {
        "path": "/shared/mask.tif",
        "count": 2,
        "label": "cell",
    }
    payload[field] = value

    with pytest.raises(TypeError, match=field):
        validate_processing_output(payload, Outputs)


def test_row_outputs_accept_one_or_many_and_preserve_order() -> None:
    one = Outputs(path=Path("/a"), count=1, label="one")
    two = Outputs(path=Path("/b"), count=2, label="two")

    normalized_one = normalize_processing_row_outputs(one, Outputs)
    assert [output.label for output in normalized_one] == ["one"]
    normalized = normalize_processing_row_outputs([one, two], Outputs)
    assert [output.label for output in normalized] == ["one", "two"]


def test_batch_outputs_require_exact_flat_or_nested_cardinality() -> None:
    one = Outputs(path=Path("/a"), count=1, label="one")
    two = Outputs(path=Path("/b"), count=2, label="two")

    flat = normalize_processing_batch_outputs(
        [one, two],
        Outputs,
        expected_rows=2,
    )
    nested = normalize_processing_batch_outputs(
        [[one, two], []],
        Outputs,
        expected_rows=2,
    )

    assert [[output.label for output in group] for group in flat] == [
        ["one"],
        ["two"],
    ]
    assert [[output.label for output in group] for group in nested] == [
        ["one", "two"],
        [],
    ]
    with pytest.raises(ValueError, match="expected 2"):
        normalize_processing_batch_outputs([one], Outputs, expected_rows=2)
    with pytest.raises(TypeError, match="mix"):
        normalize_processing_batch_outputs(
            [[one], two],
            Outputs,
            expected_rows=2,
        )


def test_plain_result_rows_share_the_same_validator() -> None:
    rows = (
        RowResultV1(
            position=0,
            row_index="row",
            outputs=(
                {
                    "path": "/shared/mask.tif",
                    "count": 2,
                    "label": "cell",
                },
            ),
        ),
    )

    [[output]] = validate_processing_result_rows(rows, Outputs)

    assert output.path == Path("/shared/mask.tif")
    assert output.count == 2
