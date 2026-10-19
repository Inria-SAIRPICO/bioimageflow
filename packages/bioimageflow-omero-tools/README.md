# bioimageflow-omero-tools

Basic dataset download and image upload for BioImageFlow, requiring only `bioimageflow-core>=0.5.0,<0.6` for discovery.

- `bioimageflow_omero_tools.omero.OmeroDownloadDataset`: download every image in a dataset to an owned OME-TIFF and report image/dataset IDs, original name, complete `TCZYX` shape and dtype.
- `bioimageflow_omero_tools.omero.OmeroUploadImage`: upload one TIFF/OME-TIFF or grayscale/RGB(A) PNG to an existing dataset, with optional image name, description and simple string metadata; return image/dataset/annotation IDs.

Both tools require explicit `host`, `username`, `dataset_id` and `port` (default `4064`).
Upload additionally requires `image`; `image_name`, `description` and `metadata` default to `None`.
Store the password in the worker-accessible OS keyring using service `bioimageflow-omero` and key `{host}:{port}:{username}`.
The BioImageFlow platform Settings page uses the same keyring contract and can create a node with the saved non-secret connection fields.
The scientific worker does not import the platform server.
Never put a password in a graph parameter, metadata, script, workflow artifact or log.

## Pixel fidelity and limits

All Z, C and T planes are preserved, with returned/downloaded axes ordered `TCZYX`.
Supported scalar dtypes are int8, uint8, int16, uint16, int32, uint32, float32 and float64.
TIFF input must contain exactly one unambiguous scalar series whose axes are a subset of `TCZYX` ending in `YX`.
Unlabelled higher-dimensional axes and interleaved TIFF sample axes are refused rather than guessed.
PNG supports grayscale uint8/uint16 and RGB/RGBA uint8, with color and alpha samples preserved as separate OMERO channels.
Palette images, transparency keys, animations, unsupported dtypes and ambiguous layouts are refused before a remote session is opened.
This transfers pixel values, dimensions and dtype, not acquisition metadata, physical units, channel names, color interpretation or original source bytes.
There is no Java/Bio-Formats importer or fileset support.

## Execution and credentials

The declared worker recipe uses CPython 3.12, OMERO.py 5.23.0, Ice 3.6.5 and pinned NumPy, Pillow, tifffile and keyring dependencies.
Official Ice wheels are selected for macOS arm64/x86_64 or glibc Linux x86_64 (glibc >=2.28).
Other platforms are refused explicitly.
Recipe selection occurs at package import, so package discovery itself is also limited to those supported hosts.
The OS keyring backend must actually be available inside the managed worker; package discovery alone cannot establish that.
Each invocation closes its own session and suppresses credential/session-bearing SDK logs while that session is active.
Ice connection and call bounds are 30 and 60 seconds respectively; these are per transport call, not a total transfer deadline.

Normal caching reuses previous download snapshots or uploaded object IDs.
Use the normal Force/Refresh execution controls to refresh a snapshot or intentionally create another remote image.
An ordinary cached Run does not upload again.
Remote uploads are not transactional: failure before the SDK returns an object ID can leave an incomplete object.
Rollback attempts only the exact image/annotation IDs returned by this invocation and never deletes by name or touches arbitrary existing objects.
Rollback diagnostics retain those owned kind/ID pairs and explicitly report deletion as unverified, because the SDK's finite wait can return before completion.
Download paths contain numeric IDs, not remote image names, and existing paths are never replaced.

## Validation

Focused package tests use synthetic arrays and fake OMERO services to check complete plane ordering, PNG values/channels, dtype preservation, schema, refusal, cleanup and secret-safe session behavior.
They do not claim actual managed-worker provisioning, OS keyring access or live-server transfer.
Live validation should create a uniquely owned dataset, upload and download known multi-Z/C/T pixels and basic PNG channels, compare all values and returned IDs, then clean only the recorded owned objects.

The [package documentation](docs/index.md) describes the supported tool contract.
