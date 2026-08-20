"""Unit tests for bioimageflow_core.tool (IOModel, ProcessingTool validation)."""

import pytest

from bioimageflow_core.tool import IOModel, ProcessingTool, RowConsumption
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
                row_consumption = RowConsumption.MAPPED
                display_name = "Bad"
                environment = EnvironmentSpec(name="test", dependencies={})

                class Outputs(IOModel):
                    result: str

    def test_process_row_only_is_valid(self):
        class Good(ProcessingTool):
            row_consumption = RowConsumption.MAPPED
            display_name = "Good Row"
            environment = EnvironmentSpec(name="test", dependencies={})

            class Outputs(IOModel):
                result: str

            def process_row(self, arguments, *, context: object | None = None):
                return self.Outputs(result="ok")

        assert Good.display_name == "Good Row"

    def test_process_batch_only_is_valid(self):
        class Good(ProcessingTool):
            row_consumption = RowConsumption.MAPPED
            display_name = "Good Batch"
            environment = EnvironmentSpec(name="test", dependencies={})

            class Outputs(IOModel):
                result: str

            def process_batch(self, arguments_list, *, context: object | None = None):
                return [self.Outputs(result="ok")]

        assert Good.display_name == "Good Batch"

    def test_missing_row_consumption_raises(self):
        with pytest.raises(TypeError, match="must explicitly declare"):
            class MissingConsumption(ProcessingTool):
                display_name = "Missing consumption"
                environment = EnvironmentSpec(name="test", dependencies={})

                class Outputs(IOModel):
                    result: str

                def process_row(self, arguments, *, context: object | None = None):
                    return self.Outputs(result="ok")

    def test_raw_string_row_consumption_raises(self):
        with pytest.raises(TypeError, match="must be a RowConsumption value"):
            class InvalidConsumption(ProcessingTool):
                row_consumption = "mapped"
                display_name = "Invalid consumption"
                environment = EnvironmentSpec(name="test", dependencies={})

                class Outputs(IOModel):
                    result: str

                def process_row(self, arguments, *, context: object | None = None):
                    return self.Outputs(result="ok")

    def test_collective_requires_process_batch(self):
        with pytest.raises(TypeError, match="must implement process_batch"):
            class InvalidCollective(ProcessingTool):
                row_consumption = RowConsumption.COLLECTIVE
                display_name = "Invalid collective"
                environment = EnvironmentSpec(name="test", dependencies={})

                class Outputs(IOModel):
                    result: str

                def process_row(self, arguments, *, context: object | None = None):
                    return self.Outputs(result="ok")

    def test_abstract_intermediate_not_validated(self):
        """A class without name or Outputs should not trigger validation."""
        class Intermediate(ProcessingTool):
            row_consumption = RowConsumption.MAPPED
            pass

        # Should not raise — no name and no Outputs

    def test_intermediate_with_name_only_not_validated(self):
        class Intermediate(ProcessingTool):
            row_consumption = RowConsumption.MAPPED
            display_name = "Intermediate"

        # Has name but no Outputs — no validation
