"""
Test the type system and compatibility checking.

Covers:
- ImageSpec creation on Annotated Path and ImageShared fields
- Compatibility checking rules (wildcard semantics)
- Type mismatch detection at graph construction time
- Layout enum properties
"""

import warnings
from pathlib import Path
from typing import Annotated, get_args

import pytest

from bioimageflow_core import (
    Connectable,
    GUIMeta,
    ImageShared,
    ImageSpec,
    Layout,
    Semantic,
    SharedArray,
    check_compatibility,
)


class TestImageSpecCreation:

    def test_annotated_path_with_all_constraints(self):
        ann = Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.INTENSITY},
                layouts={Layout.VOLUMETRIC},
                dtypes={"float32"},
                formats={".nii.gz"},
            ),
        ]
        # The annotation should carry an ImageSpec
        spec = get_args(ann)[1]
        assert isinstance(spec, ImageSpec)
        assert Semantic.INTENSITY in spec.semantics
        assert Layout.VOLUMETRIC in spec.layouts
        assert "float32" in spec.dtypes
        assert ".nii.gz" in spec.formats

    def test_annotated_path_with_semantics(self):
        ann = Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]
        spec = get_args(ann)[1]
        assert spec.semantics == {Semantic.LABEL}
        assert spec.layouts == set()  # Wildcard

    def test_annotated_path_wildcard(self):
        ann = Annotated[Path, ImageSpec()]
        spec = get_args(ann)[1]
        assert spec.semantics == set()
        assert spec.layouts == set()
        assert spec.dtypes == set()

    def test_image_shared_has_memory_format(self):
        ann = ImageShared(semantics=Semantic.PROBABILITY)
        spec = get_args(ann)[1]
        assert "memory" in spec.formats

    def test_annotated_path_with_set_of_semantics(self):
        ann = Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY, Semantic.PROBABILITY}),
        ]
        spec = get_args(ann)[1]
        assert len(spec.semantics) == 2

    def test_annotated_path_with_gui_meta(self):
        gui = GUIMeta(display_name="Input image", connectable=Connectable.BY_DEFAULT)
        ann = Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY}), gui]
        metadata = get_args(ann)[1:]
        assert metadata[0].semantics == {Semantic.INTENSITY}
        assert metadata[1] is gui

    def test_image_shared_with_gui_meta(self):
        gui = GUIMeta(display_name="Shared image", connectable=Connectable.BY_DEFAULT)
        ann = ImageShared(semantics=Semantic.LABEL, gui=gui)
        metadata = get_args(ann)[1:]
        assert metadata[0].semantics == {Semantic.LABEL}
        assert "memory" in metadata[0].formats
        assert metadata[1] is gui


class TestLayoutEnum:

    def test_ndim(self):
        assert Layout.PLANAR.ndim == 2
        assert Layout.PLANAR_CHANNEL.ndim == 3
        assert Layout.VOLUMETRIC.ndim == 3
        assert Layout.VOLUMETRIC_TIME_CHANNEL.ndim == 5

    def test_layout_values(self):
        assert Layout.PLANAR.value == "YX"
        assert Layout.VOLUMETRIC_CHANNEL.value == "CZYX"


class TestCompatibility:

    def test_both_empty_compatible(self):
        """Two wildcard specs are always compatible."""
        producer = ImageSpec()
        consumer = ImageSpec()
        assert check_compatibility(producer, consumer) is True

    def test_consumer_wildcard_accepts_any(self):
        """Consumer with empty set accepts anything."""
        producer = ImageSpec(semantics={Semantic.LABEL})
        consumer = ImageSpec()
        assert check_compatibility(producer, consumer) is True

    def test_producer_wildcard_with_warning(self):
        """Producer wildcard + consumer constrained = compatible with warning."""
        producer = ImageSpec()
        consumer = ImageSpec(semantics={Semantic.INTENSITY})
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = check_compatibility(producer, consumer)
        assert result is True
        assert any("cannot verify" in str(warning.message) for warning in w)

    def test_matching_semantics_compatible(self):
        producer = ImageSpec(semantics={Semantic.INTENSITY})
        consumer = ImageSpec(semantics={Semantic.INTENSITY, Semantic.PROBABILITY})
        assert check_compatibility(producer, consumer) is True

    def test_disjoint_semantics_incompatible(self):
        producer = ImageSpec(semantics={Semantic.LABEL})
        consumer = ImageSpec(semantics={Semantic.INTENSITY})
        assert check_compatibility(producer, consumer) is False

    def test_layout_compatibility(self):
        producer = ImageSpec(layouts={Layout.VOLUMETRIC})
        consumer = ImageSpec(layouts={Layout.VOLUMETRIC, Layout.VOLUMETRIC_CHANNEL})
        assert check_compatibility(producer, consumer) is True

    def test_layout_incompatibility(self):
        producer = ImageSpec(layouts={Layout.PLANAR})
        consumer = ImageSpec(layouts={Layout.VOLUMETRIC})
        assert check_compatibility(producer, consumer) is False

    def test_dtype_compatibility(self):
        producer = ImageSpec(dtypes={"uint8", "uint16"})
        consumer = ImageSpec(dtypes={"uint16", "float32"})
        assert check_compatibility(producer, consumer) is True

    def test_format_incompatibility(self):
        producer = ImageSpec(formats={".tif"})
        consumer = ImageSpec(formats={".nii.gz"})
        assert check_compatibility(producer, consumer) is False

    def test_multi_attribute_check(self):
        """All attributes must be compatible independently."""
        producer = ImageSpec(
            semantics={Semantic.INTENSITY},
            layouts={Layout.VOLUMETRIC},
            dtypes={"float32"},
        )
        consumer = ImageSpec(
            semantics={Semantic.INTENSITY},
            layouts={Layout.PLANAR},  # Incompatible!
            dtypes={"float32"},
        )
        assert check_compatibility(producer, consumer) is False


class TestSharedArray:

    def test_shared_array_is_picklable(self):
        import pickle

        ref = SharedArray(name="bif_test_001", shape=(64, 64), dtype="uint16")
        pickled = pickle.dumps(ref)
        unpickled = pickle.loads(pickled)
        assert unpickled.name == "bif_test_001"
        assert unpickled.shape == (64, 64)
        assert unpickled.dtype == "uint16"

    def test_shared_array_is_frozen(self):
        ref = SharedArray(name="bif_test", shape=(10,), dtype="float32")
        with pytest.raises(AttributeError):
            ref.name = "new_name"  # type: ignore[reportAttributeAccessIssue]
