"""Deterministic public transfer contracts; no real OMERO server or credential."""

from contextlib import contextmanager
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
from PIL import Image
import pytest
import tifffile

from bioimageflow_core import Arguments, ExecutionContext
from bioimageflow.validation import serialize_input_schema, serialize_output_schema
from bioimageflow_omero_tools import OmeroDownloadDataset, OmeroUploadImage
from bioimageflow_omero_tools import omero
from bioimageflow_omero_tools.connection import OmeroToolError
from bioimageflow_omero_tools.pixels import download_image, read_upload_image, upload_planes


pytestmark = pytest.mark.package_tools


@pytest.fixture
def context(tmp_path):
    return ExecutionContext(
        run_dir=tmp_path, assets_dir=tmp_path / "assets", work_dir=tmp_path / "work",
        rows_dir=tmp_path / "work/rows", row_dir=tmp_path / "work/rows/0", row_index="0",
    )


def arguments(**kwargs):
    return Arguments(host="example.invalid", port=4064, username="test-user", dataset_id=7, **kwargs)


class RemoteImage:
    def __init__(self, array, image_id=13):
        self.array = array
        self.image_id = image_id
        self.coordinates = None

    def getId(self):
        return self.image_id

    def getName(self):
        return "../../remote-name"

    def getSizeT(self):
        return self.array.shape[0]

    def getSizeC(self):
        return self.array.shape[1]

    def getSizeZ(self):
        return self.array.shape[2]

    def getSizeY(self):
        return self.array.shape[3]

    def getSizeX(self):
        return self.array.shape[4]

    def getPrimaryPixels(self):
        return self

    def get_numpy_type(self):
        return self.array.dtype

    def getPlanes(self, coordinates):
        self.coordinates = coordinates
        return (self.array[t, c, z] for z, c, t in coordinates)


def install_session(monkeypatch, connection):
    @contextmanager
    def session(*_args):
        yield connection
    monkeypatch.setattr(omero, "omero_session", session)


def test_public_schema_has_no_password_and_exposes_exact_binding_inputs():
    for tool in (OmeroDownloadDataset, OmeroUploadImage):
        schema = serialize_input_schema(tool)
        assert {"host", "port", "username", "dataset_id"} <= schema.keys()
        assert "password" not in schema
        assert schema["port"]["default"] == 4064
        assert serialize_output_schema(tool)
    assert set(serialize_output_schema(OmeroUploadImage)) == {"dataset_id", "image_id", "annotation_id"}


@pytest.mark.parametrize("dtype", ["uint8", "int8", "uint16", "int16", "uint32", "int32", "float32", "float64"])
def test_complete_tczyx_download_and_gateway_upload_order(tmp_path, dtype):
    expected = np.arange(2 * 3 * 4 * 5 * 6).reshape(2, 3, 4, 5, 6).astype(dtype)
    image = RemoteImage(expected)
    destination = tmp_path / "image.ome.tif"
    shape, actual_dtype = download_image(image, destination)
    assert shape == expected.shape
    assert actual_dtype == dtype
    with tifffile.TiffFile(destination) as tif:
        assert tif.series[0].axes == "TCZYX"
        np.testing.assert_array_equal(tif.asarray(), expected)
    actual = read_upload_image(destination)
    np.testing.assert_array_equal(actual, expected)
    planes = list(upload_planes(actual))
    for index, (z, c, t) in enumerate((z, c, t) for z in range(4) for c in range(3) for t in range(2)):
        np.testing.assert_array_equal(planes[index], expected[t, c, z])
    assert image.coordinates == [(z, c, t) for t in range(2) for c in range(3) for z in range(4)]


@pytest.mark.parametrize("mode,channels", [("L", 1), ("RGB", 3), ("RGBA", 4)])
def test_png_preserves_all_values_and_color_alpha_channels(tmp_path, mode, channels):
    shape = (5, 6) if channels == 1 else (5, 6, channels)
    expected = np.arange(np.prod(shape), dtype=np.uint8).reshape(shape)
    path = tmp_path / "image.png"
    Image.fromarray(expected).save(path)
    actual = read_upload_image(path)
    assert actual.shape == (1, channels, 1, 5, 6)
    assert actual.dtype == np.uint8
    reconstructed = actual[0, 0, 0] if mode == "L" else actual[0, :, 0].transpose(1, 2, 0)
    np.testing.assert_array_equal(reconstructed, expected)


def test_png_uint16_is_not_truncated(tmp_path):
    expected = np.array([[0, 256, 65535], [17, 40000, 42]], dtype=np.uint16)
    path = tmp_path / "gray.png"
    Image.fromarray(expected).save(path)
    actual = read_upload_image(path)
    assert actual.dtype == np.uint16
    np.testing.assert_array_equal(actual[0, 0, 0], expected)


def test_palette_png_and_ambiguous_tiff_refused_before_session(tmp_path, monkeypatch, context):
    def unexpected_session(*_args):
        pytest.fail("Invalid input must be refused before opening OMERO")
    monkeypatch.setattr(omero, "omero_session", unexpected_session)
    palette = tmp_path / "palette.png"
    Image.new("P", (4, 5)).save(palette)
    ambiguous = tmp_path / "unknown.tif"
    tifffile.imwrite(ambiguous, np.zeros((2, 5, 6), dtype=np.uint8), metadata={"axes": "QYX"})
    for path in (palette, ambiguous):
        with pytest.raises(OmeroToolError):
            OmeroUploadImage().process_row(arguments(image=path, image_name=None, description=None, metadata=None), context=context)


def test_download_uses_safe_ids_and_returns_complete_rows(monkeypatch, context):
    expected = np.arange(2 * 2 * 3 * 4 * 5, dtype=np.uint16).reshape(2, 2, 3, 4, 5)
    image = RemoteImage(expected)
    dataset = SimpleNamespace(listChildren=lambda: [image])
    install_session(monkeypatch, SimpleNamespace(getObject=lambda kind, identity: dataset))
    [output] = OmeroDownloadDataset().process_row(arguments(), context=context)
    assert output.image == context.assets_dir / "0/omero-7-13.ome.tif"
    assert output.image_name == "../../remote-name"
    assert (output.dataset_id, output.image_id, output.axes, output.shape, output.dtype) == (7, 13, "TCZYX", list(expected.shape), "uint16")
    np.testing.assert_array_equal(read_upload_image(output.image), expected)
    # An existing path is not ours to replace or remove.
    with pytest.raises(FileExistsError):
        OmeroDownloadDataset().process_row(arguments(), context=context)
    np.testing.assert_array_equal(read_upload_image(output.image), expected)


def test_incomplete_remote_plane_stream_removes_only_new_output(tmp_path):
    image = RemoteImage(np.zeros((2, 2, 2, 4, 5), dtype=np.uint16))
    image.getPlanes = lambda coordinates: iter([image.array[0, 0, 0]])
    destination = tmp_path / "partial.ome.tif"
    with pytest.raises((OmeroToolError, ValueError)):
        download_image(image, destination)
    assert not destination.exists()


def test_upload_calls_public_gateway_with_complete_shape_and_returns_ids(monkeypatch, context, tmp_path):
    expected = np.arange(2 * 3 * 4 * 5 * 6, dtype=np.uint16).reshape(2, 3, 4, 5, 6)
    path = tmp_path / "input.ome.tif"
    tifffile.imwrite(path, expected, metadata={"axes": "TCZYX"}, photometric="minisblack", ome=True)
    dataset = object()
    calls = []
    def create(planes, name, **kwargs):
        calls.append((list(planes), name, kwargs))
        return RemoteImage(expected, 41)
    install_session(monkeypatch, SimpleNamespace(getObject=lambda *args: dataset, createImageFromNumpySeq=create))
    result = OmeroUploadImage().process_row(arguments(image=path, image_name="chosen", description="description", metadata=None), context=context)
    assert (result.dataset_id, result.image_id, result.annotation_id) == (7, 41, None)
    [call] = calls
    assert call[1:] == ("chosen", {"sizeZ": 4, "sizeC": 3, "sizeT": 2, "description": "description", "dataset": dataset})
    for actual, wanted in zip(call[0], upload_planes(expected)):
        np.testing.assert_array_equal(actual, wanted)


@pytest.mark.parametrize("dtype", ["float16", "complex64", "bool"])
def test_unsupported_dtype_refused_before_opening_session(monkeypatch, context, tmp_path, dtype):
    source = tmp_path / "unsupported.tif"
    tifffile.imwrite(source, np.ones((5, 6), dtype=dtype), metadata={"axes": "YX"})
    monkeypatch.setattr(omero, "omero_session", lambda *_args: pytest.fail("No remote mutation is permitted"))
    with pytest.raises(OmeroToolError, match="dtype"):
        OmeroUploadImage().process_row(arguments(image=source, image_name=None, description=None, metadata=None), context=context)


@pytest.mark.parametrize("fail_link", [False, True])
def test_map_annotation_ids_and_owned_only_failure_rollback(monkeypatch, context, tmp_path, caplog, fail_link):
    source = tmp_path / "input.png"
    Image.fromarray(np.arange(30, dtype=np.uint8).reshape(5, 6)).save(source)
    events = []
    class Annotation:
        def __init__(self, connection):
            pass

        def setNs(self, namespace):
            events.append(("namespace", namespace))

        def setValue(self, values):
            events.append(("values", values))

        def save(self):
            events.append(("save",))

        def getId(self):
            return 43

    def link(annotation):
        events.append(("link", annotation.getId()))
        if fail_link:
            raise RuntimeError("simulated annotation permission failure")
    image = SimpleNamespace(getId=lambda: 41, linkAnnotation=link)
    connection = SimpleNamespace(
        getObject=lambda *_args: object(), createImageFromNumpySeq=lambda *_args, **_kwargs: image,
        deleteObjects=lambda kind, identities, **kwargs: events.append(("delete", kind, identities, kwargs)),
    )
    install_session(monkeypatch, connection)
    gateway, metadata = ModuleType("omero.gateway"), ModuleType("omero.constants.metadata")
    gateway.MapAnnotationWrapper = Annotation
    metadata.NSCLIENTMAPANNOTATION = "test-namespace"
    monkeypatch.setitem(sys.modules, "omero.gateway", gateway)
    monkeypatch.setitem(sys.modules, "omero.constants.metadata", metadata)
    kwargs = arguments(image=source, image_name=None, description=None, metadata={"experiment": "basic"})
    if fail_link:
        with pytest.raises(RuntimeError, match="simulated annotation"):
            OmeroUploadImage().process_row(kwargs, context=context)
        assert [event for event in events if event[0] == "delete"] == [
            ("delete", "Annotation", [43], {"wait": True}), ("delete", "Image", [41], {"wait": True}),
        ]
        assert "Annotation ID 43; deletion is unverified" in caplog.text
        assert "Image ID 41; deletion is unverified" in caplog.text
        assert "simulated annotation permission failure" not in caplog.text
    else:
        result = OmeroUploadImage().process_row(kwargs, context=context)
        assert result.annotation_id == 43 and result.image_id == 41
        assert not [event for event in events if event[0] == "delete"]
    assert ("values", [["experiment", "basic"]]) in events


def test_missing_dataset_never_creates_an_image(monkeypatch, context, tmp_path):
    source = tmp_path / "input.png"
    Image.new("L", (5, 6)).save(source)
    install_session(monkeypatch, SimpleNamespace(
        getObject=lambda *_args: None,
        createImageFromNumpySeq=lambda *_args, **_kwargs: pytest.fail("No upload without an accessible dataset"),
    ))
    with pytest.raises(OmeroToolError, match="dataset does not exist"):
        OmeroUploadImage().process_row(arguments(image=source, image_name=None, description=None, metadata=None), context=context)
