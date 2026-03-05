"""Unit tests for bioimageflow.validation."""

from pathlib import Path
from typing import Annotated

from bioimageflow_core.types import ImageSpec, Semantic, SharedArray
from bioimageflow.validation import (
    is_path_type,
    is_image_type,
    extract_image_spec,
    get_source_hash,
)


class TestIsPathType:

    def test_plain_path(self):
        assert is_path_type(Path) is True

    def test_annotated_path(self):
        ann = Annotated[Path, ImageSpec()]
        assert is_path_type(ann) is True

    def test_non_path(self):
        assert is_path_type(int) is False
        assert is_path_type(str) is False

    def test_annotated_non_path(self):
        ann = Annotated[str, "metadata"]
        assert is_path_type(ann) is False


class TestIsImageType:

    def test_annotated_with_image_spec(self):
        ann = Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]
        assert is_image_type(ann) is True

    def test_plain_path_is_not_image(self):
        assert is_image_type(Path) is False

    def test_shared_array_with_spec(self):
        ann = Annotated[SharedArray, ImageSpec(semantics={Semantic.INTENSITY})]
        assert is_image_type(ann) is True


class TestExtractImageSpec:

    def test_returns_spec(self):
        spec = ImageSpec(semantics={Semantic.LABEL})
        ann = Annotated[Path, spec]
        assert extract_image_spec(ann) is spec

    def test_returns_none_for_plain(self):
        assert extract_image_spec(int) is None


class TestGetSourceHash:

    def test_produces_hex_string(self):
        h = get_source_hash(TestGetSourceHash)
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex

    def test_deterministic(self):
        assert get_source_hash(TestGetSourceHash) == get_source_hash(TestGetSourceHash)
