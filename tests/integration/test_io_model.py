"""
Test IOModel features, MRO annotation resolution, and Passthrough.

Covers:
- IOModel._get_all_annotations() across MRO
- IOModel construction with defaults and required fields
- Inner class inheritance patterns
- Passthrough base class for DataFrameTools
- Interface type constraints (only stdlib + bioimageflow-core types)
"""

from pathlib import Path
from typing import Annotated

import pytest

from bioimageflow_core import IOModel, Semantic
from bioimageflow_core.types import ImageSpec, SharedArray


class TestIOModelBasics:

    def test_construction_with_all_fields(self):
        class M(IOModel):
            x: int
            y: str = "default"

        m = M(x=10, y="hello")
        assert m.x == 10
        assert m.y == "hello"

    def test_default_values(self):
        class M(IOModel):
            x: int
            y: str = "default"

        m = M(x=10)
        assert m.y == "default"

    def test_missing_required_raises(self):
        class M(IOModel):
            x: int

        with pytest.raises(TypeError, match="Missing required"):
            M()

    def test_unknown_field_raises(self):
        class M(IOModel):
            x: int

        with pytest.raises(TypeError, match="Unknown fields"):
            M(x=1, z=2)

    def test_repr(self):
        class M(IOModel):
            x: int
            y: str = "hi"

        m = M(x=42)
        r = repr(m)
        assert "M" in r
        assert "42" in r


class TestIOModelMRO:

    def test_get_all_annotations_single_class(self):
        class M(IOModel):
            a: int
            b: str

        ann = M._get_all_annotations()
        assert set(ann.keys()) == {"a", "b"}

    def test_get_all_annotations_with_inheritance(self):
        class Base(IOModel):
            a: int

        class Child(Base):
            b: str

        ann = Child._get_all_annotations()
        assert "a" in ann
        assert "b" in ann

    def test_child_overrides_parent_annotation(self):
        class Base(IOModel):
            x: int

        class Child(Base):
            x: str  # type: ignore[reportIncompatibleVariableOverride]  # Override type

        ann = Child._get_all_annotations()
        assert ann["x"] is str

    def test_multi_level_inheritance(self):
        class A(IOModel):
            a: int

        class B(A):
            b: float

        class C(B):
            c: str

        ann = C._get_all_annotations()
        assert set(ann.keys()) == {"a", "b", "c"}

    def test_construction_with_inherited_fields(self):
        class Base(IOModel):
            x: int

        class Child(Base):
            y: str

        c = Child(x=1, y="hi")
        assert c.x == 1
        assert c.y == "hi"


class TestIOModelInnerClasses:

    def test_inner_classes_are_independent(self):
        """Inputs/Outputs on different tools do not share fields."""

        class ToolA:
            class Inputs(IOModel):
                image: str

        class ToolB:
            class Inputs(IOModel):
                mask: str

        a_fields = set(ToolA.Inputs._get_all_annotations().keys())
        b_fields = set(ToolB.Inputs._get_all_annotations().keys())
        assert a_fields == {"image"}
        assert b_fields == {"mask"}

    def test_explicit_inner_class_inheritance(self):
        class ToolBase:
            class Inputs(IOModel):
                image: str
                quality: float = 0.5

        class ToolChild:
            class Inputs(ToolBase.Inputs):
                extra: int = 10

        ann = ToolChild.Inputs._get_all_annotations()
        assert "image" in ann
        assert "quality" in ann
        assert "extra" in ann


class TestPassthrough:

    def test_passthrough_with_extra_fields(self):
        from bioimageflow import Passthrough

        class Outputs(Passthrough):
            new_column: int

        ann = Outputs._get_all_annotations()
        assert "new_column" in ann

    def test_empty_passthrough(self):
        from bioimageflow import Passthrough

        class Outputs(Passthrough):
            pass

        # Should be valid — means "all input columns preserved, no new ones"
        ann = Outputs._get_all_annotations()
        assert isinstance(ann, dict)


class TestInterfaceTypeConstraints:

    def test_image_path_in_inputs(self):
        class Inputs(IOModel):
            img: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

        ann = Inputs._get_all_annotations()
        assert "img" in ann

    def test_image_shared_in_outputs(self):
        class Outputs(IOModel):
            result: Annotated[SharedArray, ImageSpec(semantics={Semantic.LABEL})]

        ann = Outputs._get_all_annotations()
        assert "result" in ann

    def test_stdlib_types_allowed(self):
        from pathlib import Path

        class IO(IOModel):
            path: Path
            count: int
            name: str
            ratio: float
            flag: bool

        m = IO(path=Path("/tmp"), count=5, name="test", ratio=0.5, flag=True)
        assert m.count == 5
