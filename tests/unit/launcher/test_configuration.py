import traceback
from types import SimpleNamespace

import pytest

from bioimageflow.launcher.configuration import (
    build_parsl_config,
    import_config_factory,
    inspect_parsl_config,
    verify_secret_references,
)
from bioimageflow.launcher.errors import LauncherProtocolError
from bioimageflow.launcher.types import ParslConfigRef


FACTORY = "tests.unit.launcher.config_factories:build"


def test_config_inspection_normalizes_mapping_executors_and_shared_issues() -> None:
    retries, labels, issues = inspect_parsl_config(
        SimpleNamespace(retries=1, executors={"gpu": object()}),
        binding_labels={"gpu", "cpu"},
    )

    assert retries == 1
    assert labels == ("gpu",)
    assert [issue[0] for issue in issues] == [
        "parsl-retries",
        "executor-labels",
    ]


def test_build_parsl_config_resolves_secret_outside_persisted_values() -> None:
    reference = ParslConfigRef(
        FACTORY,
        {"workers": 2},
        {"credential": "credential-ref"},
    )

    result = build_parsl_config(
        reference,
        resolver=lambda name: f"resolved-{name}",
        trusted_factories={FACTORY},
    )

    assert result == {
        "workers": 2,
        "credential": "resolved-credential-ref",
    }
    assert "resolved" not in str(reference.to_dict())


def test_factory_allowlist_fails_closed() -> None:
    with pytest.raises(LauncherProtocolError, match="not trusted"):
        import_config_factory(FACTORY, trusted_factories=set())


def test_factory_must_be_importable_and_callable() -> None:
    with pytest.raises(LauncherProtocolError, match="not callable"):
        import_config_factory("tests.unit.launcher.config_factories:not_callable")
    with pytest.raises(LauncherProtocolError, match="Cannot import"):
        import_config_factory("tests.unit.launcher.config_factories:missing")


def test_secret_references_are_verified_without_persisting_values() -> None:
    seen: list[str] = []
    reference = ParslConfigRef(
        FACTORY,
        {"workers": 1},
        {"credential": "opaque-ref"},
    )

    verify_secret_references(
        reference,
        resolver=lambda name: seen.append(name) or "value",
    )

    assert seen == ["opaque-ref"]


def test_factory_failure_does_not_leak_resolved_secret() -> None:
    secret = "resolved-secret-value"
    reference = ParslConfigRef(
        "tests.unit.launcher.config_factories:fail_with_credential",
        {},
        {"credential": "opaque-ref"},
    )

    with pytest.raises(LauncherProtocolError) as captured:
        build_parsl_config(
            reference,
            resolver=lambda _name: secret,
        )

    error = captured.value
    rendered_traceback = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert secret not in str(error)
    assert secret not in rendered_traceback
