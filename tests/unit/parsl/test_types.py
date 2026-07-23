"""Strict public Parsl value contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import json

import pytest

from bioimageflow import (
    ExecutorBinding,
    ExecutorCapabilities,
    ParslTaskPolicy,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
)


DEPENDENCY_HASH = "a" * 64


def _attestation() -> WorkerEnvironmentAttestation:
    return WorkerEnvironmentAttestation(
        name="analysis",
        dependency_hash=DEPENDENCY_HASH,
        allow_flexible_versions=False,
        core_requirement="bioimageflow-core>=0.1.7,<0.2",
    )


def _capabilities() -> ExecutorCapabilities:
    return ExecutorCapabilities(
        storage_modes=("shared_fs",),
        tool_origin_modes=("installed_module", "archive_module"),
        slot=WorkerSlotCapacity(
            cpu=4,
            gpu=1,
            memory_bytes=16 * 1024**3,
            gpu_memory_bytes=8 * 1024**3,
        ),
    )


def _binding() -> ExecutorBinding:
    return ExecutorBinding(
        label="gpu",
        environments=(_attestation(),),
        capabilities=_capabilities(),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("row_chunk_size", True),
        ("row_chunk_size", 0),
        ("max_in_flight", False),
        ("max_in_flight", 0),
    ],
)
def test_task_policy_rejects_invalid_integer_values(
    field: str,
    value: object,
) -> None:
    kwargs = {"row_chunk_size": 1, "max_in_flight": 32, field: value}
    with pytest.raises((TypeError, ValueError), match=field):
        ParslTaskPolicy(**kwargs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cpu", True),
        ("cpu", 0),
        ("gpu", False),
        ("gpu", -1),
        ("memory_bytes", 0),
        ("gpu_memory_bytes", -1),
    ],
)
def test_slot_capacity_rejects_invalid_values(
    field: str,
    value: object,
) -> None:
    kwargs = {
        "cpu": 1,
        "gpu": 0,
        "memory_bytes": None,
        "gpu_memory_bytes": None,
        field: value,
    }
    with pytest.raises((TypeError, ValueError), match=field):
        WorkerSlotCapacity(**kwargs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("storage_modes", ("object_store",)),
        ("storage_modes", ("shared_fs", "shared_fs")),
        ("tool_origin_modes", ("import_guess",)),
        ("tool_origin_modes", ["installed_module"]),
    ],
)
def test_capabilities_reject_unknown_duplicate_or_mutable_modes(
    field: str,
    value: object,
) -> None:
    kwargs = {
        "storage_modes": ("shared_fs",),
        "tool_origin_modes": ("installed_module",),
        "slot": WorkerSlotCapacity(cpu=1),
        field: value,
    }
    with pytest.raises((TypeError, ValueError), match=field):
        ExecutorCapabilities(**kwargs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("name", " padded"),
        ("dependency_hash", "A" * 64),
        ("dependency_hash", "a" * 63),
        ("allow_flexible_versions", 1),
        ("core_requirement", ""),
    ],
)
def test_attestation_rejects_noncanonical_values(
    field: str,
    value: object,
) -> None:
    kwargs = {
        "name": "analysis",
        "dependency_hash": DEPENDENCY_HASH,
        "allow_flexible_versions": False,
        "core_requirement": "bioimageflow-core>=0.1.7,<0.2",
        field: value,
    }
    with pytest.raises((TypeError, ValueError), match=field):
        WorkerEnvironmentAttestation(**kwargs)


def test_binding_rejects_duplicate_environment_identities() -> None:
    environment = _attestation()
    with pytest.raises(ValueError, match="duplicate"):
        ExecutorBinding(
            label="cpu",
            environments=(environment, environment),
            capabilities=_capabilities(),
        )


@pytest.mark.parametrize(
    "value",
    [
        ParslTaskPolicy(),
        WorkerSlotCapacity(cpu=2, memory_bytes=1024),
        _capabilities(),
        _attestation(),
        _binding(),
    ],
)
def test_public_values_round_trip_through_versioned_json(value: object) -> None:
    payload = value.to_dict()
    serialized = json.dumps(payload, sort_keys=True)
    restored = type(value).from_dict(json.loads(serialized))

    assert restored == value
    assert payload["schema"].startswith("bioimageflow.parsl.")
    assert payload["schema"].endswith(".v1")


def test_decoders_reject_missing_extra_and_unknown_schema() -> None:
    payload = _binding().to_dict()

    missing = dict(payload)
    missing.pop("label")
    with pytest.raises(ValueError, match="missing"):
        ExecutorBinding.from_dict(missing)

    extra = {**payload, "secret": "must-not-pass"}
    with pytest.raises(ValueError, match="extra"):
        ExecutorBinding.from_dict(extra)

    unknown = {**payload, "schema": "bioimageflow.parsl.executor_binding.v2"}
    with pytest.raises(ValueError, match="Unknown"):
        ExecutorBinding.from_dict(unknown)


def test_decoders_reject_non_string_keys() -> None:
    payload: dict[object, object] = _binding().to_dict()
    payload[1] = "invalid"

    with pytest.raises(TypeError, match="keys must be strings"):
        ExecutorBinding.from_dict(payload)


def test_decoders_require_json_arrays_for_tuple_fields() -> None:
    payload = _capabilities().to_dict()
    payload["storage_modes"] = ("shared_fs",)

    with pytest.raises(TypeError, match="JSON array"):
        ExecutorCapabilities.from_dict(payload)


def test_values_are_frozen() -> None:
    policy = ParslTaskPolicy()

    with pytest.raises(FrozenInstanceError):
        policy.max_in_flight = 4
