# bioimageflow-omero-tools

Two basic mapped tools transfer pixels through the public OMERO.py gateway without a platform-server dependency.

## Tools

`OmeroDownloadDataset` requires explicit `host`, `username`, `dataset_id` and optional `port=4064`.
It returns one row per image: `image` (owned OME-TIFF Path), `dataset_id`, `image_id`, `image_name`, `axes="TCZYX"`, `shape` and `dtype`.
All channels, timepoints and Z planes are retained.

`OmeroUploadImage` has the same connection/dataset fields plus required `image`, optional `image_name`, `description` and `metadata` (string-to-string map).
It accepts one unambiguous scalar TIFF/OME-TIFF series, or grayscale uint8/uint16 and RGB/RGBA uint8 PNG.
PNG color and alpha samples become separate OMERO channels with unchanged values.
It returns `dataset_id`, `image_id` and optional `annotation_id`.
Palette PNG, animations, implicit transparency keys, ambiguous TIFF axes, multiple series and unsupported dtypes are refused before a remote session is opened.
Supported scalar dtypes are int8, uint8, int16, uint16, int32, uint32, float32 and float64.
The tools transfer dimensions/dtype/pixels, not physical units, acquisition metadata, channel labels, color interpretation or original source bytes.
They do not implement a Java/Bio-Formats fileset importer.

## Credentials and worker recipe

Store credentials in the worker-accessible OS keyring service `bioimageflow-omero`, key `{host}:{port}:{username}`.
The platform Settings page uses the same contract and may populate the saved non-secret connection fields when creating a node.
No password input or platform import is required by either tool.
Do not put credentials into parameters, metadata or artifacts.
The package's host dependency is Core `>=0.5.0,<0.6`; worker-only imports are lazy.
The worker recipe uses CPython 3.12, OMERO.py 5.23.0, Ice 3.6.5 and pinned image/keyring dependencies, selecting an official Ice wheel for macOS arm64/x86_64 or glibc Linux x86_64 (glibc >=2.28).
Other platforms are unsupported.
Recipe selection at import also restricts package discovery to those supported hosts.
Actual worker OS-keyring availability and live-server access require separate validation.

## Cached execution and ownership

Cached execution reuses the captured download snapshot or returned upload IDs.
Force/Refresh intentionally obtains a new snapshot or creates another upload.
Each invocation owns and closes its own session, restores its scoped SDK log suppression, and uses finite per-call Ice timeouts.
Download filenames use numeric IDs and never overwrite a pre-existing file.
Rollback only attempts exact image/annotation IDs returned to the invocation; a failure before an SDK ID is returned can leave an incomplete object because upload is not transactional.
No name-based or unrelated-object deletion is performed.
Rollback reports the exact owned kind/ID and unverified deletion status; the SDK's finite wait does not itself prove deletion completed.

Deterministic tests use fake services and known pixels; they are not a live-transfer certification.
