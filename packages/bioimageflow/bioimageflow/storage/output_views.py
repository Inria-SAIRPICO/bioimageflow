"""Focused storage repository behavior."""

# Pyright checks the complete contract on Storage; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from typing import Literal

from .common import (
    Any,
    LINK_SCHEMA,
    Path,
    _OUTPUT_VIEW_MODES,
    errno,
    json,
    os,
    shutil,
    unicodedata,
    uuid,
)
from .models import (
    CacheCorruptionError,
    OutputViewCapability,
)
from .identity import (
    _atomic_write_json,
    _validate_node_key,
    _validate_output_view_mode,
    _validate_path_segment,
    _validate_record_id,
    validate_relative_posix_path,
)


class _OutputViewsMixin:
    def probe_output_view_mode(self, mode: str) -> OutputViewCapability:
        """Probe an output-view mode on the workflow storage filesystem."""
        value = str(mode)
        if value not in _OUTPUT_VIEW_MODES:
            return OutputViewCapability(
                mode=value,
                supported=False,
                code="invalid_mode",
                detail="The requested output-view mode is not recognized.",
            )
        if value == "none":
            return OutputViewCapability(mode=value, supported=True, code="ok")

        probe_root = self.storage_path / f".output-view-probe-{uuid.uuid4().hex}"
        operation = "create probe directory"
        try:
            probe_root.mkdir(parents=True)
            source_file = probe_root / "source.txt"
            source_file.write_text("bioimageflow-output-view-probe")
            source_dir = probe_root / "source-directory"
            source_dir.mkdir()
            (source_dir / "content.txt").write_text("bioimageflow-output-view-probe")

            if value == "pointer":
                operation = "create portable pointer"
                pointer_path = probe_root / "output.txt.bioimageflow-link.json"
                self._write_link(pointer_path, kind="file", target=source_file)
                target = self._read_link_target(pointer_path, kind="file")
                if target.read_text() != "bioimageflow-output-view-probe":
                    raise OSError(errno.EIO, "pointer verification failed")
            elif value == "symlink":
                operation = "create and read file symlink"
                file_link = probe_root / "file-link"
                os.symlink("source.txt", file_link, target_is_directory=False)
                if file_link.read_text() != "bioimageflow-output-view-probe":
                    raise OSError(errno.EIO, "file symlink verification failed")
                operation = "create and read directory symlink"
                directory_link = probe_root / "directory-link"
                os.symlink("source-directory", directory_link, target_is_directory=True)
                if (
                    directory_link / "content.txt"
                ).read_text() != "bioimageflow-output-view-probe":
                    raise OSError(errno.EIO, "directory symlink verification failed")
            elif value == "copy":
                operation = "copy and read file"
                copied_file = probe_root / "copied.txt"
                shutil.copy2(source_file, copied_file)
                if copied_file.read_text() != "bioimageflow-output-view-probe":
                    raise OSError(errno.EIO, "file copy verification failed")
                operation = "copy and read directory"
                copied_dir = probe_root / "copied-directory"
                shutil.copytree(source_dir, copied_dir)
                if (
                    copied_dir / "content.txt"
                ).read_text() != "bioimageflow-output-view-probe":
                    raise OSError(errno.EIO, "directory copy verification failed")
            elif value == "hardlink":
                operation = "create and read file hardlink"
                hardlink = probe_root / "hardlink.txt"
                os.link(source_file, hardlink)
                if hardlink.read_text() != "bioimageflow-output-view-probe":
                    raise OSError(errno.EIO, "hardlink verification failed")
                if hardlink.stat().st_ino != source_file.stat().st_ino:
                    raise OSError(errno.EIO, "hardlink identity verification failed")
            return OutputViewCapability(mode=value, supported=True, code="ok")
        except OSError as exc:
            code = self._output_view_probe_error_code(exc)
            return OutputViewCapability(
                mode=value,
                supported=False,
                code=code,
                detail=f"Could not {operation}.",
            )
        except Exception:
            return OutputViewCapability(
                mode=value,
                supported=False,
                code="io_error",
                detail=f"Could not {operation}.",
            )
        finally:
            shutil.rmtree(probe_root, ignore_errors=True)

    @staticmethod
    def _output_view_probe_error_code(
        exc: OSError,
    ) -> Literal["permission_denied", "filesystem_unsupported", "io_error"]:
        if isinstance(exc, PermissionError) or exc.errno in {errno.EACCES, errno.EPERM}:
            return "permission_denied"
        if getattr(exc, "winerror", None) in {5, 1314}:
            return "permission_denied"
        unsupported_errors = {errno.ENOSYS, errno.EINVAL}
        if hasattr(errno, "ENOTSUP"):
            unsupported_errors.add(errno.ENOTSUP)
        if hasattr(errno, "EOPNOTSUPP"):
            unsupported_errors.add(errno.EOPNOTSUPP)
        if exc.errno in unsupported_errors:
            return "filesystem_unsupported"
        return "io_error"

    def materialize_run_outputs(self, run_id: str, mode: str) -> list[Path]:
        """Materialize owned assets for one run under ``outputs/runs``."""
        mode = _validate_output_view_mode(mode)
        if mode == "none":
            return []
        safe_run_id = _validate_path_segment(run_id, label="Run ID")
        run_dir = self.run_dir(safe_run_id)
        self._load_run_metadata(safe_run_id)
        nodes_root = run_dir / "nodes"
        if not nodes_root.exists():
            return []
        materialized: list[Path] = []
        for result_path in sorted(nodes_root.rglob("result.json")):
            payload = self._load_run_node_payload(result_path)
            node_key = str(payload["node_key"])
            self._validate_run_node_view(safe_run_id, node_key)
            expected_result_path = (
                self.run_node_dir(safe_run_id, node_key) / "result.json"
            )
            if result_path.resolve() != expected_result_path.resolve():
                raise CacheCorruptionError(
                    "Run node result path does not match its node key."
                )
            destination = (
                self.outputs_root
                / "runs"
                / safe_run_id
                / "nodes"
                / _validate_node_key(node_key)
            )
            materialized.extend(
                self._materialize_node_outputs(payload, destination, mode)
            )
        return materialized

    def materialize_latest_node_outputs(self, node_key: str, mode: str) -> list[Path]:
        """Materialize owned assets for one latest node pointer under ``outputs/latest``."""
        mode = _validate_output_view_mode(mode)
        if mode == "none":
            return []
        latest_path = self._latest_node_path(node_key)
        payload = self._latest_node_payload(latest_path)
        safe_node_key = _validate_node_key(str(payload["node_key"]))
        destination = self.outputs_root / "latest" / safe_node_key
        return self._replace_latest_node_outputs(payload, destination, mode)

    def materialize_latest_outputs(self, mode: str) -> list[Path]:
        """Materialize owned assets for all latest node pointers under ``outputs/latest``."""
        mode = _validate_output_view_mode(mode)
        if mode == "none":
            return []
        if not self.latest_root.exists():
            return []
        materialized: list[Path] = []
        for latest_path in sorted(self.latest_root.rglob("*.bioimageflow-link.json")):
            payload = self._latest_node_payload(latest_path)
            node_key = str(payload["node_key"])
            destination = self.outputs_root / "latest" / _validate_node_key(node_key)
            materialized.extend(
                self._replace_latest_node_outputs(payload, destination, mode)
            )
        return materialized

    def _latest_node_path(self, node_key: str) -> Path:
        safe_node_key = _validate_node_key(node_key)
        parent = self.latest_root
        parts = safe_node_key.split("/")
        for part in parts[:-1]:
            parent = parent / part
        return parent / f"{parts[-1]}.bioimageflow-link.json"

    def _relative_target(self, pointer_path: Path, target: Path) -> str:
        return os.path.relpath(target, start=pointer_path.parent).replace(os.sep, "/")

    def _write_link(
        self,
        path: Path,
        *,
        kind: str,
        target: Path,
        digest: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "schema": LINK_SCHEMA,
            "kind": kind,
            "target": self._relative_target(path, target),
        }
        if digest is not None:
            payload["digest"] = digest
        _atomic_write_json(path, payload, stem="link")

    def _write_output_links(
        self,
        node_dir: Path,
        record_dir: Path,
        outputs: list[dict[str, Any]],
    ) -> None:
        for output in outputs:
            if output.get("kind") != "owned_asset":
                continue
            try:
                relative = validate_relative_posix_path(str(output["path"]))
            except (KeyError, ValueError) as exc:
                raise CacheCorruptionError(
                    "Run output link contains an unsafe asset path."
                ) from exc
            asset_path = record_dir / relative
            try:
                asset_path.resolve().relative_to(record_dir.resolve())
            except ValueError as exc:
                raise CacheCorruptionError(
                    f"Run output link escapes record directory: {relative}"
                ) from exc
            if not asset_path.exists():
                raise CacheCorruptionError(
                    f"Run output link target is missing: {relative}"
                )
            asset_type = str(output.get("asset_type", "file"))
            if asset_type not in {"file", "directory"}:
                raise CacheCorruptionError(
                    f"Run output link asset type is invalid: {relative}"
                )
            link_kind = "directory" if asset_type == "directory" else "file"
            link_path = node_dir / "outputs" / f"{relative}.bioimageflow-link.json"
            digest = (
                str(output.get("digest"))
                if link_kind == "file" and output.get("digest") is not None
                else None
            )
            self._write_link(
                link_path, kind=link_kind, target=asset_path, digest=digest
            )

    def _load_run_node_payload(self, result_path: Path) -> dict[str, Any]:
        if not result_path.exists():
            raise CacheCorruptionError("Run node view is missing result.json.")
        try:
            payload = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CacheCorruptionError("Run node result is invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise CacheCorruptionError("Run node result must be a JSON object.")
        return payload

    def _read_link_target(self, path: Path, *, kind: str) -> Path:
        if not path.exists():
            raise CacheCorruptionError(f"Run view pointer is missing: {path.name}")
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CacheCorruptionError("Run view pointer is invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise CacheCorruptionError("Run view pointer must be a JSON object.")
        if payload.get("schema") != LINK_SCHEMA:
            raise CacheCorruptionError("Run view pointer has an invalid schema.")
        if payload.get("kind") != kind:
            raise CacheCorruptionError("Run view pointer kind mismatch.")
        target = payload.get("target")
        if not isinstance(target, str) or target == "":
            raise CacheCorruptionError("Run view pointer target is invalid.")
        target_path = Path(target)
        if target_path.is_absolute():
            raise CacheCorruptionError("Run view pointer target must be relative.")
        resolved = (path.parent / target_path).resolve()
        try:
            resolved.relative_to(self.storage_path.resolve())
        except ValueError as exc:
            raise CacheCorruptionError(
                "Run view pointer target escapes storage root."
            ) from exc
        return resolved

    def _latest_node_payload(self, latest_path: Path) -> dict[str, Any]:
        node_dir = self._read_link_target(latest_path, kind="directory")
        try:
            node_dir.relative_to(self.runs_root.resolve())
        except ValueError as exc:
            raise CacheCorruptionError(
                "Latest node pointer escapes views/runs."
            ) from exc
        result_path = node_dir / "result.json"
        payload = self._load_run_node_payload(result_path)
        node_key = _validate_node_key(str(payload["node_key"]))
        run_id = _validate_path_segment(str(payload["run_id"]), label="Run ID")
        expected_node_dir = self.run_node_dir(run_id, node_key).resolve()
        if node_dir != expected_node_dir:
            raise CacheCorruptionError("Latest node pointer target mismatch.")
        return self._validate_run_node_view(run_id, node_key)

    def _materialize_node_outputs(
        self,
        payload: dict[str, Any],
        node_destination: Path,
        mode: str,
    ) -> list[Path]:
        planned = self._plan_node_outputs(payload, simplify_latest=False)
        outputs_destination = node_destination / "outputs"
        self._remove_output_view_path(outputs_destination)
        materialized: list[Path] = []
        for asset_path, relative, output in planned:
            destination = self._materialized_destination(
                outputs_destination / relative, mode
            )
            self._materialize_path(asset_path, destination, mode, output=output)
            materialized.append(destination)
        return materialized

    def _replace_latest_node_outputs(
        self,
        payload: dict[str, Any],
        node_destination: Path,
        mode: str,
    ) -> list[Path]:
        planned = self._plan_node_outputs(payload, simplify_latest=True)
        node_destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = (
            node_destination.parent / f".{node_destination.name}.{uuid.uuid4().hex}.tmp"
        )
        backup = (
            node_destination.parent
            / f".{node_destination.name}.{uuid.uuid4().hex}.backup"
        )
        moved_previous = False
        installed = False
        try:
            temporary.mkdir()
            relative_destinations: list[Path] = []
            for asset_path, relative, output in planned:
                destination = self._materialized_destination(temporary / relative, mode)
                self._materialize_path(asset_path, destination, mode, output=output)
                relative_destinations.append(destination.relative_to(temporary))

            if node_destination.exists() or node_destination.is_symlink():
                os.replace(node_destination, backup)
                moved_previous = True
            try:
                os.replace(temporary, node_destination)
                installed = True
            except BaseException:
                if moved_previous:
                    os.replace(backup, node_destination)
                    moved_previous = False
                raise
            if moved_previous:
                self._remove_output_view_path(backup)
                moved_previous = False
            return [node_destination / relative for relative in relative_destinations]
        finally:
            self._remove_output_view_path(temporary)
            if (
                moved_previous
                and not installed
                and not (node_destination.exists() or node_destination.is_symlink())
            ):
                os.replace(backup, node_destination)
                moved_previous = False
            if not moved_previous:
                self._remove_output_view_path(backup)

    def _plan_node_outputs(
        self,
        payload: dict[str, Any],
        *,
        simplify_latest: bool,
    ) -> list[tuple[Path, str, dict[str, Any]]]:
        result_key = str(payload["result_key"])
        record_id = _validate_record_id(str(payload["record_id"]))
        record_dir = self.result_dir(result_key) / "records" / record_id
        manifest = self._load_record_manifest(result_key, record_id)
        planned: list[tuple[Path, str, dict[str, Any]]] = []
        portable_paths: dict[str, str] = {}
        for output in manifest.outputs:
            if output.get("kind") != "owned_asset":
                continue
            source_relative = validate_relative_posix_path(str(output["path"]))
            mapped_relative = source_relative
            if simplify_latest and source_relative.startswith("assets/"):
                mapped_relative = source_relative.removeprefix("assets/")
            mapped_relative = validate_relative_posix_path(mapped_relative)
            if simplify_latest:
                portable = unicodedata.normalize("NFC", mapped_relative).casefold()
                for existing_portable, existing_path in portable_paths.items():
                    if (
                        portable == existing_portable
                        or portable.startswith(f"{existing_portable}/")
                        or existing_portable.startswith(f"{portable}/")
                    ):
                        raise CacheCorruptionError(
                            "Output view paths collide after latest mapping: "
                            f"{existing_path!r} and {mapped_relative!r}."
                        )
                portable_paths[portable] = mapped_relative
            asset_path = record_dir / source_relative
            try:
                asset_path.resolve().relative_to(record_dir.resolve())
            except ValueError as exc:
                raise CacheCorruptionError(
                    f"Output view asset escapes record directory: {source_relative}"
                ) from exc
            if not asset_path.exists():
                raise CacheCorruptionError(
                    f"Output view target is missing: {source_relative}"
                )
            asset_type = str(output.get("asset_type", "file"))
            if asset_type not in {"file", "directory"}:
                raise CacheCorruptionError(
                    f"Output view asset type is invalid: {source_relative}"
                )
            planned.append((asset_path, mapped_relative, output))
        return planned

    @staticmethod
    def _materialized_destination(destination: Path, mode: str) -> Path:
        if mode == "pointer":
            return destination.with_name(f"{destination.name}.bioimageflow-link.json")
        return destination

    @staticmethod
    def _remove_output_view_path(path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    def _materialize_path(
        self,
        source: Path,
        destination: Path,
        mode: str,
        *,
        output: dict[str, Any],
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if mode == "pointer":
            kind = "directory" if source.is_dir() else "file"
            digest = (
                str(output["digest"])
                if kind == "file" and output.get("digest")
                else None
            )
            self._write_link(destination, kind=kind, target=source, digest=digest)
            if self._read_link_target(destination, kind=kind) != source.resolve():
                raise CacheCorruptionError("Output pointer target mismatch.")
            return
        if mode == "symlink":
            target = os.path.relpath(source, start=destination.parent)
            os.symlink(target, destination, target_is_directory=source.is_dir())
            return
        if mode == "copy":
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
            return
        if mode == "hardlink":
            if source.is_dir():
                raise OSError(
                    "Output view hardlink mode does not support directory assets."
                )
            os.link(source, destination)
            return
        raise ValueError(f"Invalid output_view mode '{mode}'.")

    def _validate_link(
        self,
        path: Path,
        *,
        kind: str,
        target: Path,
        digest: str | None = None,
    ) -> None:
        if not path.exists():
            raise CacheCorruptionError(f"Run view pointer is missing: {path.name}")
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CacheCorruptionError("Run view pointer is invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise CacheCorruptionError("Run view pointer must be a JSON object.")
        if payload.get("schema") != LINK_SCHEMA:
            raise CacheCorruptionError("Run view pointer has an invalid schema.")
        if payload.get("kind") != kind:
            raise CacheCorruptionError("Run view pointer kind mismatch.")
        expected_target = self._relative_target(path, target)
        if payload.get("target") != expected_target:
            raise CacheCorruptionError("Run view pointer target mismatch.")
        if digest is not None and payload.get("digest") != digest:
            raise CacheCorruptionError("Run view pointer digest mismatch.")
