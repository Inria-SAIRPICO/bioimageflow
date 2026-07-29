from datetime import timedelta
from pathlib import Path
from pathlib import PurePosixPath

import pytest

from bioimageflow.launcher import (
    OrchestratorLaunchConfig,
    PSIJLaunchConfig,
    ParslConfigRef,
)
from bioimageflow.launcher.types import launch_config_from_dict


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


def test_psij_launch_config_round_trips_exact_json_values() -> None:
    launch = PSIJLaunchConfig(
        executor="slurm",
        walltime=timedelta(hours=2),
        queue="cpu-short",
        project="BIOIMAGE/2026",
        cpu_cores=4,
        work_dir=PurePosixPath("/cluster/project/work"),
        hard_cancel_after=30,
    )

    encoded = launch.to_dict()

    assert encoded == {
        "backend": "psij",
        "executor": "slurm",
        "walltime_seconds": 7200.0,
        "queue": "cpu-short",
        "project": "BIOIMAGE/2026",
        "cpu_cores": 4,
        "work_dir": "/cluster/project/work",
        "hard_cancel_after": 30.0,
    }
    assert PSIJLaunchConfig.from_dict(encoded) == launch
    assert launch_config_from_dict(encoded) == launch


@pytest.mark.parametrize("executor", ["oar", "local", "SLURM", "", object()])
def test_psij_launch_config_rejects_unknown_executor(executor: object) -> None:
    with pytest.raises(ValueError):
        PSIJLaunchConfig(
            executor=executor,  # type: ignore[arg-type]
            walltime=timedelta(minutes=5),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("queue", "cpu\n#SBATCH --account=other"),
        ("queue", "cpu short"),
        ("project", "--account=other"),
        ("project", "BIO;rm"),
    ],
)
def test_psij_launch_config_rejects_scheduler_fragments(
    field: str,
    value: str,
) -> None:
    kwargs = {field: value}
    with pytest.raises(ValueError):
        PSIJLaunchConfig(
            executor="pbs",
            walltime=timedelta(minutes=5),
            **kwargs,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "value",
    [
        timedelta(0),
        timedelta(seconds=-1),
        300,
        "00:05:00",
    ],
)
def test_psij_launch_config_rejects_invalid_walltime(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        PSIJLaunchConfig(
            executor="lsf",
            walltime=value,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "value",
    [
        "relative/work",
        "/cluster/../other",
        "//cluster/work",
        "/cluster/work/",
        "/cluster/\x00work",
    ],
)
def test_psij_launch_config_rejects_unsafe_work_dir(value: str) -> None:
    with pytest.raises(ValueError):
        PSIJLaunchConfig(
            executor="slurm",
            walltime=timedelta(minutes=5),
            work_dir=value,  # type: ignore[arg-type]
        )


def test_local_launch_config_has_no_scheduler_aliases() -> None:
    for backend in ("slurm", "pbs", "lsf", "oar", "psij"):
        with pytest.raises(ValueError):
            OrchestratorLaunchConfig(backend=backend)  # type: ignore[arg-type]


@pytest.mark.parametrize("cpu_cores", [0, -1, 1.5, True])
def test_psij_launch_config_requires_positive_integer_cores(
    cpu_cores: object,
) -> None:
    with pytest.raises(ValueError):
        PSIJLaunchConfig(
            executor="slurm",
            walltime=timedelta(minutes=5),
            cpu_cores=cpu_cores,  # type: ignore[arg-type]
        )


def test_psij_launch_config_codec_rejects_unknown_or_missing_fields() -> None:
    encoded = PSIJLaunchConfig(
        executor="slurm",
        walltime=timedelta(minutes=5),
    ).to_dict()

    with pytest.raises(ValueError, match="requires exactly"):
        PSIJLaunchConfig.from_dict({**encoded, "native_options": {}})
    encoded.pop("queue")
    with pytest.raises(ValueError, match="requires exactly"):
        PSIJLaunchConfig.from_dict(encoded)
