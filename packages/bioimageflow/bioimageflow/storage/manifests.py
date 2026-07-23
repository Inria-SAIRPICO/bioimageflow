"""Immutable cache-record and current-pointer manifests."""

from __future__ import annotations

from dataclasses import dataclass

from .common import (
    Any,
    CURRENT_SCHEMA,
    Path,
    RECORD_SCHEMA,
    _INTEGER_RE,
    _RECORD_MANIFEST_FIELDS,
    _UNSIGNED_INTEGER_RE,
    datetime,
    timezone,
)
from .models import (
    CacheCorruptionError,
)
from .identity import (
    _file_sha256,
    _validate_record_id,
    _validate_sha256_digest,
    asset_digest_and_size,
    make_record_id,
    result_shard_parts,
    validate_relative_posix_path,
)


@dataclass(frozen=True)
class RecordManifest:
    """Structured manifest helper for a reusable record."""

    result_key: str
    record_id: str
    dataframe_digest: str
    outputs: list[dict[str, Any]]
    schema: str = RECORD_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "result_key": self.result_key,
            "record_id": self.record_id,
            "dataframe": {
                "path": "dataframe.parquet",
                "digest": self.dataframe_digest,
            },
            "outputs": list(self.outputs),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RecordManifest":
        if not isinstance(value, dict):
            raise CacheCorruptionError("Record manifest must be a JSON object.")
        unknown = set(value) - _RECORD_MANIFEST_FIELDS
        if unknown:
            raise CacheCorruptionError(
                f"Record manifest contains unknown fields: {sorted(unknown)!r}"
            )
        if value.get("schema") != RECORD_SCHEMA:
            raise CacheCorruptionError("Invalid record manifest schema.")
        dataframe = value.get("dataframe")
        if not isinstance(dataframe, dict):
            raise CacheCorruptionError("Record manifest is missing dataframe metadata.")
        if dataframe.get("path") != "dataframe.parquet":
            raise CacheCorruptionError("Record manifest has an invalid dataframe path.")
        outputs = value.get("outputs")
        if not isinstance(outputs, list):
            raise CacheCorruptionError("Record manifest outputs must be a list.")
        try:
            return cls(
                result_key=str(value["result_key"]),
                record_id=str(value["record_id"]),
                dataframe_digest=str(dataframe["digest"]),
                outputs=[dict(output) for output in outputs],
                schema=str(value["schema"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CacheCorruptionError("Invalid record manifest shape.") from exc

    def validate(
        self, record_dir: Path, *, expected_result_key: str | None = None
    ) -> None:
        if self.schema != RECORD_SCHEMA:
            raise CacheCorruptionError("Invalid record manifest schema.")
        try:
            result_shard_parts(self.result_key)
            _validate_record_id(self.record_id)
        except ValueError as exc:
            raise CacheCorruptionError(
                "Record manifest contains unsafe identifiers."
            ) from exc
        if expected_result_key is not None and self.result_key != expected_result_key:
            raise CacheCorruptionError("Record manifest result key mismatch.")
        if record_dir.name != self.record_id:
            raise CacheCorruptionError("Record manifest record ID mismatch.")
        _validate_sha256_digest(self.dataframe_digest, label="dataframe")
        dataframe = record_dir / "dataframe.parquet"
        if not dataframe.exists():
            raise CacheCorruptionError("Record is missing dataframe.parquet.")
        try:
            dataframe.resolve().relative_to(record_dir.resolve())
        except ValueError as exc:
            raise CacheCorruptionError(
                "Record dataframe escapes record directory."
            ) from exc
        if _file_sha256(dataframe) != self.dataframe_digest:
            raise CacheCorruptionError("Record dataframe digest mismatch.")
        for output in self.outputs:
            self._validate_output(record_dir, output)
        if make_record_id(self.to_dict()) != self.record_id:
            raise CacheCorruptionError(
                "Record ID does not match record manifest content."
            )

    def _validate_output(self, record_dir: Path, output: dict[str, Any]) -> None:
        kind = output.get("kind")
        if kind == "owned_asset":
            try:
                relative = validate_relative_posix_path(str(output["path"]))
            except (KeyError, ValueError) as exc:
                raise CacheCorruptionError(
                    "Record manifest contains an unsafe asset path."
                ) from exc
            asset_path = record_dir / relative
            try:
                asset_path.resolve().relative_to(record_dir.resolve())
            except ValueError as exc:
                raise CacheCorruptionError(
                    f"Asset escapes record directory: {relative}"
                ) from exc
            if not asset_path.exists():
                raise CacheCorruptionError(f"Record asset is missing: {relative}")
            if "size" not in output or "digest" not in output:
                raise CacheCorruptionError(
                    f"Record asset is missing size or digest: {relative}"
                )
            asset_type = str(output.get("asset_type", "file"))
            if asset_type not in {"file", "directory"}:
                raise CacheCorruptionError(f"Record asset type is invalid: {relative}")
            if asset_type == "file" and not asset_path.is_file():
                raise CacheCorruptionError(f"Record asset is not a file: {relative}")
            if asset_type == "directory" and not asset_path.is_dir():
                raise CacheCorruptionError(
                    f"Record asset is not a directory: {relative}"
                )
            try:
                expected_size = int(output["size"])
            except (TypeError, ValueError) as exc:
                raise CacheCorruptionError(
                    f"Record asset size is invalid: {relative}"
                ) from exc
            actual_size, actual_digest = asset_digest_and_size(asset_path)
            if actual_size != expected_size:
                raise CacheCorruptionError(f"Record asset size mismatch: {relative}")
            _validate_sha256_digest(str(output["digest"]), label="asset")
            if actual_digest != output["digest"]:
                raise CacheCorruptionError(f"Record asset digest mismatch: {relative}")
            if output.get("asset_role") == "shared_array":
                self._validate_shared_array_asset(
                    asset_path, relative, output, asset_type=asset_type
                )
            return
        if kind == "external_path":
            if not isinstance(output.get("path"), str) or output["path"] == "":
                raise CacheCorruptionError("Record manifest external path is invalid.")
            if output.get("identity") not in {"path"}:
                raise CacheCorruptionError(
                    "Record manifest external path identity is invalid."
                )
            return
        if kind == "scalar_output":
            self._validate_scalar_output(output)
            return
        raise CacheCorruptionError(f"Unsupported record output kind: {kind!r}")

    def _validate_scalar_output(self, output: dict[str, Any]) -> None:
        if (
            not isinstance(output.get("output_column"), str)
            or output["output_column"] == ""
        ):
            raise CacheCorruptionError(
                "Record manifest scalar output column is invalid."
            )
        if not isinstance(output.get("row_index"), str) or output["row_index"] == "":
            raise CacheCorruptionError(
                "Record manifest scalar output row index is invalid."
            )
        value = output.get("value")
        if not isinstance(value, dict):
            raise CacheCorruptionError(
                "Record manifest scalar output value is invalid."
            )
        kind = value.get("kind")
        scalar_value = value.get("value")
        if kind == "null":
            if scalar_value is not None:
                raise CacheCorruptionError(
                    "Record manifest null scalar output is invalid."
                )
            return
        if kind == "bool":
            if not isinstance(scalar_value, bool):
                raise CacheCorruptionError(
                    "Record manifest bool scalar output is invalid."
                )
            return
        if kind == "signed_integer":
            if (
                not isinstance(scalar_value, str)
                or _INTEGER_RE.fullmatch(scalar_value) is None
            ):
                raise CacheCorruptionError(
                    "Record manifest integer scalar output is invalid."
                )
            return
        if kind == "unsigned_integer":
            if (
                not isinstance(scalar_value, str)
                or _UNSIGNED_INTEGER_RE.fullmatch(scalar_value) is None
            ):
                raise CacheCorruptionError(
                    "Record manifest unsigned integer scalar output is invalid."
                )
            return
        if kind == "float":
            if not isinstance(scalar_value, str) or scalar_value == "":
                raise CacheCorruptionError(
                    "Record manifest float scalar output is invalid."
                )
            if scalar_value not in {"NaN", "Infinity", "-Infinity"}:
                try:
                    float(scalar_value)
                except ValueError as exc:
                    raise CacheCorruptionError(
                        "Record manifest float scalar output is invalid."
                    ) from exc
            return
        if kind == "string":
            if not isinstance(scalar_value, str):
                raise CacheCorruptionError(
                    "Record manifest string scalar output is invalid."
                )
            return
        if kind == "datetime":
            if not isinstance(scalar_value, str) or scalar_value == "":
                raise CacheCorruptionError(
                    "Record manifest datetime scalar output is invalid."
                )
            return
        raise CacheCorruptionError(
            f"Unsupported record scalar output value kind: {kind!r}"
        )

    def _validate_shared_array_asset(
        self,
        asset_path: Path,
        relative: str,
        output: dict[str, Any],
        *,
        asset_type: str,
    ) -> None:
        if asset_type != "file":
            raise CacheCorruptionError(
                f"Record shared-array asset must be a file: {relative}"
            )
        if not relative.startswith("assets/shm/"):
            raise CacheCorruptionError(
                f"Record shared-array asset path is invalid: {relative}"
            )
        array = output.get("array")
        if not isinstance(array, dict):
            raise CacheCorruptionError(
                f"Record shared-array metadata is missing: {relative}"
            )
        if not isinstance(array.get("column"), str) or array["column"] == "":
            raise CacheCorruptionError(
                f"Record shared-array column is invalid: {relative}"
            )
        if not isinstance(array.get("row_index"), str):
            raise CacheCorruptionError(
                f"Record shared-array row index is invalid: {relative}"
            )
        if array.get("format") != "npy":
            raise CacheCorruptionError(
                f"Record shared-array format is invalid: {relative}"
            )
        if array.get("order") != "C":
            raise CacheCorruptionError(
                f"Record shared-array order is invalid: {relative}"
            )
        shape = array.get("shape")
        if not isinstance(shape, list) or not all(
            isinstance(item, int) and item >= 0 for item in shape
        ):
            raise CacheCorruptionError(
                f"Record shared-array shape is invalid: {relative}"
            )
        dtype = array.get("dtype")
        if not isinstance(dtype, str) or dtype == "":
            raise CacheCorruptionError(
                f"Record shared-array dtype is invalid: {relative}"
            )
        try:
            import numpy as np

            loaded = np.load(asset_path, allow_pickle=False)
        except Exception as exc:
            raise CacheCorruptionError(
                f"Record shared-array asset is unreadable: {relative}"
            ) from exc
        if list(loaded.shape) != shape:
            raise CacheCorruptionError(
                f"Record shared-array shape mismatch: {relative}"
            )
        if str(loaded.dtype) != dtype:
            raise CacheCorruptionError(
                f"Record shared-array dtype mismatch: {relative}"
            )
        if not loaded.flags.c_contiguous:
            raise CacheCorruptionError(
                f"Record shared-array order mismatch: {relative}"
            )


@dataclass(frozen=True)
class CurrentPointer:
    """Structured helper for ``current.json``."""

    result_key: str
    record_id: str
    manifest: str
    attempt_id: str
    run_id: str
    policy: str = "first-valid"
    schema: str = CURRENT_SCHEMA
    selected_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        try:
            result_shard_parts(self.result_key)
            _validate_record_id(self.record_id)
            manifest = validate_relative_posix_path(self.manifest)
        except ValueError as exc:
            raise CacheCorruptionError("Invalid current pointer identifiers.") from exc
        return {
            "schema": self.schema,
            "result_key": self.result_key,
            "record_id": self.record_id,
            "manifest": manifest,
            "policy": self.policy,
            "selected_at": self.selected_at or datetime.now(timezone.utc).isoformat(),
            "selected_by": {
                "attempt_id": self.attempt_id,
                "run_id": self.run_id,
            },
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CurrentPointer":
        if not isinstance(value, dict):
            raise CacheCorruptionError("Current pointer must be a JSON object.")
        if value.get("schema") != CURRENT_SCHEMA:
            raise CacheCorruptionError("Invalid current pointer schema.")
        selected_by = value.get("selected_by") or {}
        if not isinstance(selected_by, dict):
            raise CacheCorruptionError("Current pointer selected_by must be an object.")
        try:
            pointer = cls(
                result_key=str(value["result_key"]),
                record_id=str(value["record_id"]),
                manifest=str(value["manifest"]),
                policy=str(value.get("policy", "first-valid")),
                attempt_id=str(selected_by.get("attempt_id", "")),
                run_id=str(selected_by.get("run_id", "")),
                selected_at=str(value.get("selected_at", "")),
            )
            pointer.to_dict()
        except (KeyError, TypeError, ValueError) as exc:
            raise CacheCorruptionError("Invalid current pointer shape.") from exc
        if pointer.policy != "first-valid":
            raise CacheCorruptionError("Invalid current pointer policy.")
        if pointer.manifest != f"records/{pointer.record_id}/manifest.json":
            raise CacheCorruptionError(
                "Current pointer manifest path does not match record ID."
            )
        return pointer
