"""Unit tests for bioimageflow_core.tool (IOModel, ProcessingTool validation)."""

import pytest

from bioimageflow_core.tool import IOModel, ProcessingTool, BaseTool
from bioimageflow_core.arguments import Arguments
from bioimageflow_core.environment import EnvironmentSpec


class TestIOModel:

    def test_required_field(self):
        class M(IOModel):
            x: int

        m = M(x=5)
        assert m.x == 5

    def test_missing_required_raises(self):
        class M(IOModel):
            x: int

        with pytest.raises(TypeError, match="Missing required field: 'x'"):
            M()

    def test_default_used(self):
        class M(IOModel):
            x: int = 10

        m = M()
        assert m.x == 10

    def test_unknown_field_raises(self):
        class M(IOModel):
            x: int

        with pytest.raises(TypeError, match="Unknown fields"):
            M(x=1, y=2)

    def test_inheritance(self):
        class Base(IOModel):
            a: int

        class Child(Base):
            b: str

        c = Child(a=1, b="hi")
        assert c.a == 1
        assert c.b == "hi"

    def test_get_all_annotations_includes_parents(self):
        class Base(IOModel):
            a: int

        class Child(Base):
            b: str

        ann = Child._get_all_annotations()
        assert "a" in ann
        assert "b" in ann

    def test_repr(self):
        class M(IOModel):
            x: int

        r = repr(M(x=42))
        assert "M" in r
        assert "42" in r


class TestProcessingToolValidation:

    def test_missing_process_methods_raises(self):
        with pytest.raises(TypeError, match="must implement process_row or process_batch"):
            class Bad(ProcessingTool):
                name = "bad"
                environment = EnvironmentSpec(name="test", dependencies={})

                class Outputs(IOModel):
                    result: str

    def test_process_row_only_is_valid(self):
        class Good(ProcessingTool):
            name = "good_row"
            environment = EnvironmentSpec(name="test", dependencies={})

            class Outputs(IOModel):
                result: str

            def process_row(self, arguments):
                return self.Outputs(result="ok")

        assert Good.name == "good_row"

    def test_process_batch_only_is_valid(self):
        class Good(ProcessingTool):
            name = "good_batch"
            environment = EnvironmentSpec(name="test", dependencies={})

            class Outputs(IOModel):
                result: str

            def process_batch(self, arguments_list):
                return [self.Outputs(result="ok")]

        assert Good.name == "good_batch"

    def test_abstract_intermediate_not_validated(self):
        """A class without name or Outputs should not trigger validation."""
        class Intermediate(ProcessingTool):
            pass

        # Should not raise — no name and no Outputs

    def test_intermediate_with_name_only_not_validated(self):
        class Intermediate(ProcessingTool):
            name = "intermediate"

        # Has name but no Outputs — no validation
