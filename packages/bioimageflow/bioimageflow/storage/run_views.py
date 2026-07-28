"""Focused storage repository behavior."""

# Pyright checks the complete contract on Storage; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from .common import (
    Any,
    Path,
    RUN_NODE_RESULT_SCHEMA,
    RUN_SCHEMA,
    Sequence,
    datetime,
    json,
    timezone,
)
from .models import (
    CacheCorruptionError,
)
from .allocation import RunAllocationLock
from .identity import (
    _atomic_write_json,
    _bioimageflow_version,
    _validate_node_key,
    _validate_path_segment,
    _validate_record_id,
    result_shard_parts,
    validate_relative_posix_path,
)


class _RunViewsMixin:
    def start_run_metadata(
        self,
        run_id: str,
        *,
        workflow_identity: str,
        engine: str,
        target_nodes: Sequence[str],
        launcher_reserved: bool,
        started_at: str | None = None,
    ) -> Path:
        """Create one canonical running view under the global run-ID guard."""
        safe_run_id = _validate_path_segment(run_id, label="Run ID")
        launcher_dir = (
            self.storage_path / "launcher" / "v1" / "runs" / safe_run_id
        )
        run_dir = self.run_dir(safe_run_id)
        with RunAllocationLock(self.storage_path):
            if launcher_reserved:
                submission_path = launcher_dir / "submission.json"
                if (
                    launcher_dir.is_symlink()
                    or not launcher_dir.is_dir()
                    or submission_path.is_symlink()
                    or not submission_path.is_file()
                ):
                    raise CacheCorruptionError(
                        "Submitted canonical run has no matching launcher reservation."
                    )
                try:
                    submission = json.loads(submission_path.read_text())
                except (OSError, json.JSONDecodeError) as exc:
                    raise CacheCorruptionError(
                        "Launcher reservation metadata is invalid."
                    ) from exc
                if (
                    not isinstance(submission, dict)
                    or submission.get("run_id") != safe_run_id
                    or submission.get("storage_root")
                    != str(self.storage_path.resolve(strict=False))
                ):
                    raise CacheCorruptionError(
                        "Launcher reservation does not match the canonical run."
                    )
            elif launcher_dir.exists() or launcher_dir.is_symlink():
                raise CacheCorruptionError(
                    "Run ID is already reserved by a submitted workflow."
                )
            if run_dir.exists() or run_dir.is_symlink():
                raise CacheCorruptionError(
                    f"Run ID {safe_run_id!r} already has a canonical view."
                )
            run_dir.mkdir(parents=True)
            try:
                return self.write_run_metadata(
                    safe_run_id,
                    workflow_identity=workflow_identity,
                    engine=engine,
                    status="running",
                    target_nodes=target_nodes,
                    started_at=started_at,
                )
            except BaseException:
                try:
                    run_dir.rmdir()
                except OSError:
                    pass
                raise

    def write_run_metadata(
        self,
        run_id: str,
        *,
        workflow_identity: str,
        engine: str,
        status: str,
        target_nodes: Sequence[str],
        started_at: str | None = None,
        completed_at: str | None = None,
    ) -> Path:
        """Write workflow-level run metadata."""
        run_path = self.run_dir(run_id) / "run.json"
        payload = {
            "schema": RUN_SCHEMA,
            "run_id": _validate_path_segment(run_id, label="Run ID"),
            "workflow_identity": workflow_identity,
            "storage_path": str(self.storage_path),
            "started_at": started_at or datetime.now(timezone.utc).isoformat(),
            "completed_at": completed_at,
            "engine": engine,
            "bioimageflow_version": _bioimageflow_version(),
            "target_nodes": [str(node) for node in target_nodes],
            "status": status,
        }
        _atomic_write_json(run_path, payload, stem="run")
        return run_path

    def finalize_run_metadata(
        self,
        run_id: str,
        *,
        status: str,
        update_latest_success: bool,
        completed_at: str | None = None,
    ) -> Path:
        """Idempotently finalize one canonical run for launcher recovery."""
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError(
                "Canonical run status must be succeeded, failed, or cancelled."
            )
        if update_latest_success != (status == "succeeded"):
            raise ValueError(
                "Only succeeded canonical runs update latest-success."
            )
        current = self._load_run_metadata(run_id)
        current_status = current["status"]
        if current_status == status:
            run_path = self.run_dir(run_id) / "run.json"
        elif current_status == "running":
            run_path = self.write_run_metadata(
                run_id,
                workflow_identity=str(current["workflow_identity"]),
                engine=str(current["engine"]),
                status=status,
                target_nodes=list(current["target_nodes"]),
                started_at=str(current["started_at"]),
                completed_at=completed_at
                or datetime.now(timezone.utc).isoformat(),
            )
        else:
            raise CacheCorruptionError(
                f"Canonical run is already terminal as {current_status!r}."
            )
        if update_latest_success:
            self.update_latest_success_run(run_id)
        return run_path

    def write_run_node_result(
        self,
        run_id: str,
        node_key: str,
        *,
        result_key: str,
        record_id: str,
        cache_hit: bool,
        provenance: dict[str, Any] | None = None,
    ) -> Path:
        """Write a run-local view over one immutable record."""
        safe_run_id = _validate_path_segment(run_id, label="Run ID")
        safe_node_key = _validate_node_key(node_key)
        result_shard_parts(result_key)
        record_id = _validate_record_id(record_id)
        manifest = self._load_record_manifest(result_key, record_id)
        record_dir = self.result_dir(result_key) / "records" / record_id
        node_dir = self.run_node_dir(run_id, node_key)
        canonical = self._relative_target(node_dir / "result.json", record_dir)
        payload = {
            "schema": RUN_NODE_RESULT_SCHEMA,
            "run_id": safe_run_id,
            "node_key": safe_node_key,
            "result_key": result_key,
            "record_id": record_id,
            "cache_hit": bool(cache_hit),
            "canonical": canonical,
            "outputs": list(manifest.outputs),
        }
        if provenance is not None:
            payload["provenance"] = provenance
        result_path = node_dir / "result.json"
        _atomic_write_json(result_path, payload, stem="result")
        self._write_link(
            node_dir / "record.bioimageflow-link.json",
            kind="directory",
            target=record_dir,
        )
        self._write_output_links(node_dir, record_dir, manifest.outputs)
        return result_path

    def update_latest_node(self, node_key: str, run_id: str) -> Path:
        """Atomically point ``views/latest/<node-key>`` at a run-node view."""
        target = self.run_node_dir(run_id, node_key)
        self._validate_run_node_view(run_id, node_key)
        latest_path = self._latest_node_path(node_key)
        self._write_link(latest_path, kind="directory", target=target)
        return latest_path

    def update_latest_success_run(self, run_id: str) -> Path:
        """Atomically point ``views/runs/latest-success`` at a successful run view."""
        target = self.run_dir(run_id)
        run = self._load_run_metadata(run_id)
        if run.get("status") != "succeeded":
            raise CacheCorruptionError(
                "Latest successful run must reference a successful run."
            )
        latest_path = self.runs_root / "latest-success.bioimageflow-link.json"
        self._write_link(latest_path, kind="directory", target=target)
        return latest_path

    def latest_success_run_id(self) -> str | None:
        """Return the latest successful run ID from ``views/runs`` if present."""
        latest_path = self.runs_root / "latest-success.bioimageflow-link.json"
        if not latest_path.exists():
            return None
        run_dir = self._read_link_target(latest_path, kind="directory")
        try:
            run_dir.relative_to(self.runs_root.resolve())
        except ValueError as exc:
            raise CacheCorruptionError(
                "Latest successful run pointer escapes views/runs."
            ) from exc
        run_id = run_dir.name
        expected_run_dir = self.run_dir(run_id).resolve()
        if run_dir != expected_run_dir:
            raise CacheCorruptionError("Latest successful run pointer target mismatch.")
        run = self._load_run_metadata(run_id)
        if run.get("status") != "succeeded":
            raise CacheCorruptionError(
                "Latest successful run must reference a successful run."
            )
        return run_id

    def _load_run_metadata(self, run_id: str) -> dict[str, Any]:
        run_path = self.run_dir(run_id) / "run.json"
        if not run_path.exists():
            raise CacheCorruptionError("Run metadata is missing.")
        try:
            payload = json.loads(run_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CacheCorruptionError("Run metadata is invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise CacheCorruptionError("Run metadata must be a JSON object.")
        if payload.get("schema") != RUN_SCHEMA:
            raise CacheCorruptionError("Run metadata has an invalid schema.")
        if payload.get("run_id") != run_id:
            raise CacheCorruptionError("Run metadata run ID mismatch.")
        if not isinstance(payload.get("status"), str) or payload["status"] == "":
            raise CacheCorruptionError("Run metadata status is invalid.")
        return payload

    def _validate_run_node_view(self, run_id: str, node_key: str) -> dict[str, Any]:
        node_dir = self.run_node_dir(run_id, node_key)
        result_path = node_dir / "result.json"
        payload = self._load_run_node_payload(result_path)
        if payload.get("schema") != RUN_NODE_RESULT_SCHEMA:
            raise CacheCorruptionError("Run node result has an invalid schema.")
        if payload.get("run_id") != run_id:
            raise CacheCorruptionError("Run node result run ID mismatch.")
        if payload.get("node_key") != node_key:
            raise CacheCorruptionError("Run node result node key mismatch.")
        result_key = str(payload.get("result_key", ""))
        record_id = str(payload.get("record_id", ""))
        try:
            result_shard_parts(result_key)
            _validate_record_id(record_id)
        except ValueError as exc:
            raise CacheCorruptionError(
                "Run node result contains invalid identifiers."
            ) from exc
        manifest = self._load_record_manifest(result_key, record_id)
        record_dir = self.result_dir(result_key) / "records" / record_id
        if payload.get("outputs") != manifest.outputs:
            raise CacheCorruptionError(
                "Run node result outputs do not match the selected record manifest."
            )
        provenance = payload.get("provenance")
        if provenance is not None and not isinstance(provenance, dict):
            raise CacheCorruptionError("Run node provenance must be a JSON object.")
        expected_canonical = self._relative_target(result_path, record_dir)
        if payload.get("canonical") != expected_canonical:
            raise CacheCorruptionError("Run node result canonical path mismatch.")
        record_link = node_dir / "record.bioimageflow-link.json"
        if not record_link.exists():
            raise CacheCorruptionError("Run node record pointer is missing.")
        self._validate_link(record_link, kind="directory", target=record_dir)
        for output in manifest.outputs:
            if output.get("kind") != "owned_asset":
                continue
            relative = validate_relative_posix_path(str(output["path"]))
            asset_type = output.get("asset_type")
            if asset_type not in {"file", "directory"}:
                raise CacheCorruptionError("Run node output asset type is invalid.")
            link_kind = "directory" if asset_type == "directory" else "file"
            digest = (
                str(output.get("digest"))
                if link_kind == "file" and output.get("digest") is not None
                else None
            )
            self._validate_link(
                node_dir / "outputs" / f"{relative}.bioimageflow-link.json",
                kind=link_kind,
                target=record_dir / relative,
                digest=digest,
            )
        return payload
