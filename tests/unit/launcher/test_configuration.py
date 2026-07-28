import pytest

from bioimageflow.launcher.configuration import (
    build_parsl_config,
    import_config_factory,
    verify_secret_references,
)
from bioimageflow.launcher.errors import LauncherProtocolError
from bioimageflow.launcher.types import ParslConfigRef


FACTORY = "tests.unit.launcher.config_factories:build"


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
        import_config_factory(
            "tests.unit.launcher.config_factories:not_callable"
        )
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
