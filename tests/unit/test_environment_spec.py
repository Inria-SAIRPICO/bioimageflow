import pytest

from bioimageflow_core import EnvironmentSpec


def test_environment_spec_accepts_exact_pip_and_conda_pins() -> None:
    spec = EnvironmentSpec(
        name="exact",
        dependencies={
            "pip": ["numpy==2.4.2"],
            "conda": ["bioimageit::simglib=0.1.2", "cellpose==4.0.8"],
        },
    )

    assert spec.dependencies["pip"] == ["numpy==2.4.2"]


def test_environment_spec_rejects_unversioned_pip_dependency() -> None:
    with pytest.raises(ValueError, match="exact version pin"):
        EnvironmentSpec(name="bad", dependencies={"pip": ["numpy"]})


def test_environment_spec_rejects_unversioned_conda_dependency() -> None:
    with pytest.raises(ValueError, match="exact version pin"):
        EnvironmentSpec(name="bad", dependencies={"conda": ["bioimageit::atlas"]})


def test_environment_spec_rejects_ranges_by_default() -> None:
    with pytest.raises(ValueError, match="exact version pin"):
        EnvironmentSpec(name="bad", dependencies={"pip": ["numpy>=2,<3"]})


def test_environment_spec_allows_explicit_ranges_when_flexible() -> None:
    spec = EnvironmentSpec(
        name="flexible",
        dependencies={"pip": ["numpy>=2,<3"], "conda": ["tensorflow>=2,<3"]},
        allow_flexible_versions=True,
    )

    assert spec.allow_flexible_versions is True


def test_environment_spec_rejects_bare_names_even_when_flexible() -> None:
    with pytest.raises(ValueError, match="explicit version constraint"):
        EnvironmentSpec(
            name="bad",
            dependencies={"pip": ["numpy"]},
            allow_flexible_versions=True,
        )


def test_environment_spec_allows_direct_and_local_dependencies() -> None:
    spec = EnvironmentSpec(
        name="local",
        dependencies={
            "pip": ["bioimageflow-core @ file:///repo/packages/bioimageflow-core"],
            "local": [
                {
                    "name": "bioimageflow-core",
                    "path": "/repo/packages/bioimageflow-core",
                    "editable": True,
                }
            ],
        },
    )

    assert "pip" in spec.dependencies


def test_environment_spec_allows_empty_dependency_specs() -> None:
    EnvironmentSpec(name="empty", dependencies={})
    EnvironmentSpec(name="empty-lists", dependencies={"pip": [], "conda": []})
