import copy
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from bioimageflow_core import EnvironmentSpec
from bioimageflow_core.defaults import snapshot_value


def test_environment_spec_accepts_exact_pip_and_conda_pins() -> None:
    spec = EnvironmentSpec(
        name="exact",
        dependencies={
            "pip": ["numpy==2.4.2"],
            "conda": ["bioimageit::simglib==0.1.2", "cellpose==4.0.8"],
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


@pytest.mark.parametrize("requirement", [
    'sample[test]==1.2.3; python_version >= "3.9"',
    "sample===nightly",
    "sample==1.2.3,!=1.2.4",
    "sample@https://example.invalid/sample.whl",
])
def test_environment_spec_parses_complete_pip_requirement_and_preserves_text(requirement):
    spec = EnvironmentSpec("complete-pip", {"pip": [requirement]})
    assert spec.dependencies == {"pip": [requirement]}


@pytest.mark.parametrize("flexible", [False, True], ids=["strict", "flexible"])
@pytest.mark.parametrize("section, requirement", [
    ("pip", "numpy=="),
    ("pip", "numpy=>=2"),
    ("pip", 'numpy==2; python_version => "3.9"'),
    ("conda", "numpy=="),
    ("conda", "numpy=>=2"),
])
def test_environment_spec_rejects_malformed_or_empty_constraints(section, requirement, flexible):
    with pytest.raises(ValueError):
        EnvironmentSpec("malformed", {section: [requirement]}, flexible)


@pytest.mark.parametrize("section, requirement", [
    ("pip", "numpy==2.*"),
    ("conda", "conda-forge::numpy=2.5"),
    ("conda", "numpy==2.*"),
    ("conda", "numpy=2.5.3=py312_*"),
])
def test_environment_spec_flexible_constraints_require_explicit_opt_in(section, requirement):
    with pytest.raises(ValueError, match="exact version pin"):
        EnvironmentSpec("strict", {section: [requirement]})
    flexible = EnvironmentSpec("flexible", {section: [requirement]}, allow_flexible_versions=True)
    assert flexible.dependencies == {section: [requirement]}


@pytest.mark.parametrize("requirement", [
    "conda-forge::numpy==2.5.3",
    "conda-forge::numpy=2.5.3=py312_0",
    "conda-forge::numpy==2.5.3=py312_0",
    pytest.param("conda-forge/linux-64::numpy==2.5.3", id="channel-subdirectory"),
    pytest.param("https://example.invalid/conda/linux-64::numpy==2.5.3", id="https-channel"),
])
def test_environment_spec_preserves_exact_conda_channel_version_and_build(requirement):
    spec = EnvironmentSpec("exact-conda", {"conda": [requirement]})
    assert spec.dependencies == {"conda": [requirement]}


def _nested_recipe():
    return {
        "python": "3.12", "pip": ["numpy==2.5.3"], "channels": ["conda-forge"],
        "local": [{"name": "example", "path": Path("/tmp/held-local-project"),
                   "editable": True, "extras": ["base"]}],
    }


def test_environment_spec_detaches_constructor_recipe_and_each_public_projection():
    recipe = _nested_recipe()
    expected = copy.deepcopy(recipe)
    spec = EnvironmentSpec("held", recipe)
    recipe["pip"][0] = "numpy==2.5.4"
    recipe["channels"].append("original-edit")
    recipe["local"][0]["extras"].append("original-edit")
    first = spec.dependencies
    second = spec.dependencies
    assert first == second == expected
    first["python"] = "3.13"
    first["pip"].append("sample==1.0")
    first["channels"].append("returned-edit")
    first["local"][0]["editable"] = False
    first["local"][0]["extras"].append("returned-edit")
    assert second == expected and spec.dependencies == expected
    assert spec == EnvironmentSpec("held", expected)
    with pytest.raises(FrozenInstanceError):
        spec.dependencies = {}


def test_environment_spec_snapshot_reconstructs_equal_detached_recipe():
    recipe = _nested_recipe()
    recipe["conda"] = ["conda-forge::numpy=2.5"]
    expected = copy.deepcopy(recipe)
    spec = EnvironmentSpec("snapshot", recipe, allow_flexible_versions=True)
    captured = snapshot_value(spec)
    assert isinstance(captured, EnvironmentSpec) and captured is not spec
    assert captured == spec and captured.name == "snapshot" and captured.allow_flexible_versions
    recipe["local"][0]["extras"].append("original-edit")
    captured_public = captured.dependencies
    captured_public["local"][0]["extras"].append("captured-edit")
    original_public = spec.dependencies
    original_public["channels"].append("returned-edit")
    assert captured.dependencies == expected and spec.dependencies == expected and captured == spec
