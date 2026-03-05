"""Unit tests for bioimageflow_core.types."""

import warnings
from pathlib import Path
from typing import Annotated, get_args

import pytest

from bioimageflow_core.types import (
    ImageSpec,
    Layout,
    Semantic,
    SharedArray,
    ImagePath,
    ImageShared,
    check_compatibility,
    _normalize_param,
)


class TestLayout:

    def test_ndim_planar(self):
        assert Layout.PLANAR.ndim == 2

    def test_ndim_volumetric_time_channel(self):
        assert Layout.VOLUMETRIC_TIME_CHANNEL.ndim == 5

    def test_all_layouts_have_positive_ndim(self):
        for layout in Layout:
            assert layout.ndim > 0


class TestNormalizeParam:

    def test_none_gives_empty_set(self):
        assert _normalize_param(None) == set()

    def test_single_value_wrapped(self):
        assert _normalize_param(Semantic.LABEL) == {Semantic.LABEL}

    def test_set_passthrough(self):
        s = {Semantic.LABEL, Semantic.BINARY}
        assert _normalize_param(s) is s

    def test_list_converted(self):
        assert _normalize_param([Semantic.LABEL]) == {Semantic.LABEL}

    def test_tuple_converted(self):
        assert _normalize_param((Layout.PLANAR,)) == {Layout.PLANAR}


class TestImageFactories:

    def test_image_path_is_annotated_path(self):
        ann = ImagePath(semantics=Semantic.INTENSITY)
        base = get_args(ann)[0]
        assert base is Path

    def test_image_shared_has_memory_format(self):
        ann = ImageShared(semantics=Semantic.LABEL)
        spec = get_args(ann)[1]
        assert "memory" in spec.formats

    def test_image_path_empty_spec(self):
        ann = ImagePath()
        spec = get_args(ann)[1]
        assert spec.semantics == set()


class TestCheckCompatibility:

    def test_exact_match(self):
        producer = ImageSpec(semantics={Semantic.LABEL})
        consumer = ImageSpec(semantics={Semantic.LABEL})
        assert check_compatibility(producer, consumer) is True

    def test_disjoint_semantics(self):
        producer = ImageSpec(semantics={Semantic.LABEL})
        consumer = ImageSpec(semantics={Semantic.INTENSITY})
        assert check_compatibility(producer, consumer) is False

    def test_consumer_wildcard_accepts_anything(self):
        """Empty consumer set means 'any' — always compatible."""
        producer = ImageSpec(semantics={Semantic.LABEL})
        consumer = ImageSpec(semantics=set())
        assert check_compatibility(producer, consumer) is True

    def test_producer_wildcard_warns(self):
        """Empty producer set warns but doesn't reject."""
        producer = ImageSpec(semantics=set())
        consumer = ImageSpec(semantics={Semantic.LABEL})
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = check_compatibility(producer, consumer)
        assert result is True
        assert any("cannot verify" in str(warning.message) for warning in w)

    def test_partial_overlap_is_compatible(self):
        producer = ImageSpec(semantics={Semantic.LABEL, Semantic.BINARY})
        consumer = ImageSpec(semantics={Semantic.LABEL, Semantic.INTENSITY})
        assert check_compatibility(producer, consumer) is True

    def test_layout_mismatch(self):
        producer = ImageSpec(layouts={Layout.PLANAR})
        consumer = ImageSpec(layouts={Layout.VOLUMETRIC})
        assert check_compatibility(producer, consumer) is False

    def test_multiple_attributes_all_must_pass(self):
        producer = ImageSpec(
            semantics={Semantic.LABEL},
            layouts={Layout.PLANAR},
        )
        consumer = ImageSpec(
            semantics={Semantic.LABEL},
            layouts={Layout.VOLUMETRIC},
        )
        assert check_compatibility(producer, consumer) is False


class TestSharedArray:

    def test_frozen(self):
        sa = SharedArray(name="test", shape=(10, 10), dtype="float32")
        with pytest.raises(AttributeError):
            sa.name = "other"  # type: ignore[reportAttributeAccessIssue]

    def test_fields(self):
        sa = SharedArray(name="seg", shape=(3, 256, 256), dtype="uint8")
        assert sa.name == "seg"
        assert sa.shape == (3, 256, 256)
        assert sa.dtype == "uint8"
