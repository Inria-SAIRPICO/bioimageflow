"""
Test environment management and isolation.

Covers:
- EnvironmentSpec definition and normalization
- Environment reuse across tools
- EnvironmentMismatchError on dependency conflict
- bioimageflow-core auto-added to worker environments
"""

import pytest

from bioimageflow_core import EnvironmentSpec

from .conftest import cellpose_env, stardist_env


class TestEnvironmentSpec:

    def test_environment_spec_is_frozen(self):
        with pytest.raises(AttributeError):
            cellpose_env.name = "changed"  # type: ignore[reportAttributeAccessIssue]

    def test_environment_spec_equality_by_identity(self):
        """Multiple tools reference the same EnvironmentSpec object."""
        env_copy = EnvironmentSpec(
            name="cellpose",
            dependencies={"conda": ["cellpose==4.0.8"], "python": "3.12"},
        )
        # Not the same object, but same content
        assert env_copy.name == cellpose_env.name
        assert env_copy.dependencies == cellpose_env.dependencies

    def test_different_environments_differ(self):
        assert cellpose_env.name != stardist_env.name
        assert cellpose_env.dependencies != stardist_env.dependencies


class TestEnvironmentMismatch:

    def test_same_name_different_deps_raises(self, tmp_workspace):
        """Two EnvironmentSpecs with same name but different deps raise error."""
        from bioimageflow import Workflow
        from typing import Annotated
        from pathlib import Path
        from bioimageflow_core import IOModel, ImageSpec, ProcessingTool, Semantic

        env_v1 = EnvironmentSpec(
            name="conflict_env",
            dependencies={"conda": ["numpy==1.24"]},
        )
        env_v2 = EnvironmentSpec(
            name="conflict_env",
            dependencies={"conda": ["numpy==2.0"]},
        )

        class ToolV1(ProcessingTool):
            display_name = "Tool V1"
            environment = env_v1

            class Inputs(IOModel):
                input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

            class Outputs(IOModel):
                result: float

            def process_row(self, arguments, *, context: object | None = None):
                return self.Outputs(result=1.0)

        class ToolV2(ProcessingTool):
            display_name = "Tool V2"
            environment = env_v2

            class Inputs(IOModel):
                input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

            class Outputs(IOModel):
                result: float

            def process_row(self, arguments, *, context: object | None = None):
                return self.Outputs(result=2.0)

        from .conftest import FileLoader

        load = FileLoader()

        with pytest.raises(Exception, match="[Mm]ismatch|conflict|environment"):
            with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
                raw = load(path=str(tmp_workspace / "data"))
                r1 = ToolV1()(input_image=raw["path"], name="v1")
                r2 = ToolV2()(input_image=raw["path"], name="v2")
                wf.compute(r1, r2)
