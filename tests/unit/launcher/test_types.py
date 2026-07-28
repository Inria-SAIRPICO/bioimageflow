from pathlib import Path

import pytest

from bioimageflow.launcher import OrchestratorLaunchConfig, ParslConfigRef


def test_parsl_config_ref_round_trips_json_safe_values() -> None:
    reference = ParslConfigRef(
        "example.config:build",
        {"labels": ["cpu", "gpu"], "workers": 2},
        {"credential": "BIF_TEST_CREDENTIAL"},
    )

    restored = ParslConfigRef.from_dict(reference.to_dict())

    assert restored.to_dict() == reference.to_dict()
    with pytest.raises(TypeError):
        restored.kwargs["workers"] = 4  # type: ignore[index]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"password": "literal"},
        {"nested": {"api_key": "literal"}},
        {"value": float("nan")},
        {"value": object()},
    ],
)
def test_parsl_config_ref_rejects_unsafe_kwargs(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        ParslConfigRef("example.config:build", kwargs)


def test_secret_argument_is_not_duplicated_in_kwargs() -> None:
    with pytest.raises(ValueError):
        ParslConfigRef(
            "example.config:build",
            {"credential": "literal"},
            {"credential": "BIF_TEST_CREDENTIAL"},
        )


def test_launch_config_normalizes_work_dir(tmp_path: Path) -> None:
    launch = OrchestratorLaunchConfig(
        backend="manual",
        work_dir=tmp_path / "work",
        hard_cancel_after=2,
    ).normalized()

    assert launch.work_dir == (tmp_path / "work").resolve()
    assert launch.hard_cancel_after == 2.0
    assert OrchestratorLaunchConfig.from_dict(launch.to_dict()) == launch


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), "2"])
def test_launch_config_rejects_invalid_hard_cancel(value: object) -> None:
    with pytest.raises(ValueError):
        OrchestratorLaunchConfig(hard_cancel_after=value)  # type: ignore[arg-type]
