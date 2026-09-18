"""Conda channel translation at the BioImageFlow to Wetlands boundary."""

import pytest
from wetlands import EnvironmentSpec as WetlandsEnvironmentSpec

from bioimageflow.env_manager import WetlandsEnvManager, _translate_conda
from bioimageflow_spot_tools.atlas import atlas_env


@pytest.mark.parametrize(
    ("dependencies", "declared_channels", "expected_conda", "expected_channels"),
    [
        (["numpy==2"], None, ("numpy==2",), ("conda-forge",)),
        (
            ["bioimageit::atlas>=0"],
            None,
            ("atlas>=0",),
            ("bioimageit", "conda-forge"),
        ),
        (
            ["first::alpha==1", "second::beta==2"],
            None,
            ("alpha==1", "beta==2"),
            ("first", "second", "conda-forge"),
        ),
        (["numpy==2"], ["custom"], ("numpy==2",), ("custom",)),
        (
            ["bioimageit::atlas>=0"],
            ["custom", "conda-forge"],
            ("atlas>=0",),
            ("custom", "conda-forge", "bioimageit"),
        ),
        (
            ["bioimageit::atlas>=0"],
            ["custom"],
            ("atlas>=0",),
            ("custom", "bioimageit"),
        ),
        (
            ["bioimageit::atlas>=0", "bioimageit::other==1"],
            ["custom", "custom", "bioimageit"],
            ("atlas>=0", "other==1"),
            ("custom", "bioimageit"),
        ),
        (
            ["conda-forge::numpy==2", "bioimageit::atlas>=0"],
            None,
            ("numpy==2", "atlas>=0"),
            ("conda-forge", "bioimageit"),
        ),
    ],
)
def test_translate_conda_channels(
    dependencies: list[str],
    declared_channels: list[str] | None,
    expected_conda: tuple[str, ...],
    expected_channels: tuple[str, ...],
) -> None:
    assert _translate_conda(dependencies, declared_channels) == (
        expected_conda,
        expected_channels,
    )


def test_atlas_channels_reach_wetlands_environment_spec() -> None:
    manager = object.__new__(WetlandsEnvManager)
    manager._bioimageflow_core_dependency = "bioimageflow-core==0.4.0"

    translated = manager._to_wetlands_spec(atlas_env)

    assert isinstance(translated, WetlandsEnvironmentSpec)
    assert translated.python == ">=3.9"
    assert translated.conda == ("atlas>=0",)
    assert translated.channels == ("bioimageit", "conda-forge")
    assert translated.pypi == ("bioimageflow-core==0.4.0",)
    assert "channels" not in atlas_env.dependencies
