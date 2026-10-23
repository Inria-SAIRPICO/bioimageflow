"""Small public OMERO transfers; host discovery imports only BioImageFlow Core."""

import logging
from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments, Category, Connectable, ExecutionContext, GUIMeta, ImageSpec,
    IOModel, ProcessingTool, RowConsumption,
)

from .connection import OmeroToolError, connection_fields, omero_session, positive_id, runtime_scratch
from .environments import OMERO_ENV
from .pixels import AXES, download_image, read_upload_image, upload_planes


class _ConnectionInputs(IOModel):
    host: Annotated[str, GUIMeta(connectable=Connectable.NEVER)]
    username: Annotated[str, GUIMeta(connectable=Connectable.NEVER)]
    port: Annotated[int, GUIMeta(connectable=Connectable.NEVER)] = 4064
    dataset_id: int


def _dataset(connection: Any, dataset_id: int) -> Any:
    dataset = connection.getObject("Dataset", dataset_id)
    if dataset is None:
        raise OmeroToolError("OMERO dataset does not exist or is not accessible.")
    return dataset


def _remove_owned_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logging.getLogger(__name__).warning("Owned OMERO output cleanup failed.")


class OmeroDownloadDataset(ProcessingTool):
    """Download every dataset image as a lossless, complete TCZYX OME-TIFF."""

    display_name = "OMERO Download Dataset"
    documentation = (
        "Download all dataset images without dropping channels, timepoints or Z planes. "
        "The worker reads the password from the local bioimageflow-omero keyring. "
        "Cached results are reused normally; force execution to refresh a remote snapshot."
    )
    category = Category.CONVERSION
    tags = ["omero", "download", "image"]
    row_consumption = RowConsumption.MAPPED
    environment = OMERO_ENV

    class Inputs(_ConnectionInputs):
        pass

    class Outputs(IOModel):
        image: Annotated[Path, ImageSpec()]
        dataset_id: int
        image_id: int
        image_name: str
        axes: str
        shape: list[int]
        dtype: str

    def process_row(self, arguments: Arguments, *, context: ExecutionContext | None = None) -> Any:
        connection_fields(arguments)
        dataset_id = positive_id(arguments.dataset_id, "Dataset ID")
        scratch = runtime_scratch(context)
        assert context is not None
        # A row-owned asset directory avoids collisions across mapped rows.
        destination_dir = context.assets_dir / scratch.name
        destination_dir.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []
        try:
            with omero_session(arguments, context) as connection:
                dataset = _dataset(connection, dataset_id)
                outputs = []
                for image in dataset.listChildren():
                    image_id = positive_id(image.getId(), "Image ID")
                    destination = destination_dir / f"omero-{dataset_id}-{image_id}.ome.tif"
                    shape, dtype = download_image(image, destination)
                    created.append(destination)
                    outputs.append(self.Outputs(
                        image=destination, dataset_id=dataset_id, image_id=image_id,
                        image_name=str(image.getName()), axes=AXES, shape=list(shape), dtype=dtype,
                    ))
                return outputs
        except BaseException:
            for path in created:
                _remove_owned_file(path)
            raise


def _upload_metadata(arguments: Arguments) -> tuple[str | None, str | None, dict[str, str]]:
    name, description, metadata = arguments.image_name, arguments.description, arguments.metadata
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise OmeroToolError("Image name must be a non-empty string when supplied.")
    if description is not None and not isinstance(description, str):
        raise OmeroToolError("Description must be a string when supplied.")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict) or any(
        not isinstance(key, str) or not key.strip() or not isinstance(value, str)
        for key, value in metadata.items()
    ):
        raise OmeroToolError("Metadata must contain non-empty string keys and string values.")
    return name, description, metadata


class OmeroUploadImage(ProcessingTool):
    """Upload one basic TIFF/OME-TIFF or grayscale/RGB(A) PNG into a dataset."""

    display_name = "OMERO Upload Image"
    documentation = (
        "Upload one lossless TIFF/OME-TIFF or grayscale/RGB(A) PNG image. "
        "Optional metadata is a string-to-string map annotation. "
        "Cached results reuse the existing returned IDs; force execution to upload another image."
    )
    category = Category.CONVERSION
    tags = ["omero", "upload", "image"]
    row_consumption = RowConsumption.MAPPED
    environment = OMERO_ENV

    class Inputs(_ConnectionInputs):
        image: Annotated[Path, ImageSpec()]
        image_name: str | None = None
        description: str | None = None
        metadata: dict[str, str] | None = None

    class Outputs(IOModel):
        dataset_id: int
        image_id: int
        annotation_id: int | None = None

    def process_row(self, arguments: Arguments, *, context: ExecutionContext | None = None) -> Any:
        connection_fields(arguments)
        dataset_id = positive_id(arguments.dataset_id, "Dataset ID")
        runtime_scratch(context)
        name, description, metadata = _upload_metadata(arguments)
        path = Path(arguments.image)
        array = read_upload_image(path)
        size_t, size_c, size_z, _size_y, _size_x = array.shape
        with omero_session(arguments, context) as connection:
            dataset = _dataset(connection, dataset_id)
            image_id = annotation_id = None
            try:
                image = connection.createImageFromNumpySeq(
                    upload_planes(array), name or path.name,
                    sizeZ=size_z, sizeC=size_c, sizeT=size_t,
                    description=description, dataset=dataset,
                )
                if image is None:
                    raise OmeroToolError("OMERO did not return an uploaded image.")
                image_id = positive_id(image.getId(), "Uploaded image ID")
                if metadata:
                    from omero.constants.metadata import NSCLIENTMAPANNOTATION  # pyright: ignore[reportMissingImports]
                    from omero.gateway import MapAnnotationWrapper  # pyright: ignore[reportMissingImports]

                    annotation = MapAnnotationWrapper(connection)
                    annotation.setNs(NSCLIENTMAPANNOTATION)
                    annotation.setValue([[key, value] for key, value in metadata.items()])
                    annotation.save()
                    annotation_id = positive_id(annotation.getId(), "Annotation ID")
                    image.linkAnnotation(annotation)
                return self.Outputs(dataset_id=dataset_id, image_id=image_id, annotation_id=annotation_id)
            except BaseException:
                # Never search by name or delete pre-existing remote objects.
                for kind, object_id in (("Annotation", annotation_id), ("Image", image_id)):
                    if object_id is not None:
                        try:
                            connection.deleteObjects(kind, [object_id], wait=True)
                        except Exception:
                            logging.getLogger(__name__).warning(
                                "Owned OMERO rollback failed for %s ID %d; deletion is unverified.", kind, object_id,
                            )
                        else:
                            # SDK wait=True has a finite poll budget and does
                            # not fail on timeout; return is not a deletion oracle.
                            logging.getLogger(__name__).warning(
                                "Owned OMERO rollback requested for %s ID %d; deletion is unverified.", kind, object_id,
                            )
                raise
