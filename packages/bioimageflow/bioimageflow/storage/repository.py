"""Focused storage repository behavior."""

from __future__ import annotations

from .common import (
    Path,
    datetime,
    json,
    os,
    re,
    timezone,
    uuid,
)
from .models import (
    CacheCorruptionError,
)
from .identity import (
    _atomic_write_json,
    _sha256_token,
    _validate_node_key,
    _validate_path_segment,
    _validate_record_id,
    result_shard_parts,
    validate_relative_posix_path,
)
from .manifests import (
    CurrentPointer,
    RecordManifest,
)


class _RepositoryMixin:
    def __init__(self, storage_path: str | Path) -> None:
        self.storage_path = Path(storage_path)

    @property
    def cache_root(self) -> Path:
        return self.storage_path / "cache" / "v1"

    @property
    def views_root(self) -> Path:
        return self.storage_path / "views"

    @property
    def runs_root(self) -> Path:
        return self.views_root / "runs"

    @property
    def latest_root(self) -> Path:
        return self.views_root / "latest"

    @property
    def outputs_root(self) -> Path:
        return self.storage_path / "outputs"

    def result_dir(self, result_key: str) -> Path:
        first, second = result_shard_parts(result_key)
        return self.cache_root / "results" / first / second / result_key

    def run_dir(self, run_id: str) -> Path:
        return self.runs_root / _validate_path_segment(run_id, label="Run ID")

    def run_node_dir(self, run_id: str, node_key: str) -> Path:
        return self.run_dir(run_id) / "nodes" / _validate_node_key(node_key)

    def new_attempt_id(self) -> str:
        return f"att_{uuid.uuid4().hex}"

    def start_cache_attempt(
        self,
        result_key: str,
        attempt_id: str,
        *,
        run_id: str,
        node_key: str,
        tool_identity: str,
        engine: str,
        invocation_id: str | None = None,
    ) -> Path:
        """Write running metadata for one private reusable attempt."""
        result_shard_parts(result_key)
        if not re.fullmatch(r"att_[0-9a-f]{32}", attempt_id):
            raise ValueError(f"Invalid attempt ID: {attempt_id!r}")
        if not re.fullmatch(r"run_[0-9a-f]{32}", run_id):
            raise ValueError(f"Invalid run ID: {run_id!r}")
        if invocation_id is not None and not re.fullmatch(
            r"inv_[0-9a-f]{32}",
            invocation_id,
        ):
            raise ValueError(f"Invalid invocation ID: {invocation_id!r}")
        if not tool_identity or not engine:
            raise ValueError("Tool identity and engine must be non-empty.")
        node_key = _validate_node_key(node_key)
        attempt_dir = (
            self.result_dir(result_key) / "attempts" / attempt_id
        )
        attempt_dir.mkdir(parents=True, exist_ok=True)
        if attempt_dir.is_symlink():
            raise CacheCorruptionError("Attempt directory must not be a symlink.")
        payload = {
            "schema": "bioimageflow.cache.attempt.v1",
            "result_key": result_key,
            "attempt_id": attempt_id,
            "run_id": run_id,
            "node_key": node_key,
            "invocation_id": invocation_id,
            "tool_identity": tool_identity,
            "engine": engine,
            "worker_identity": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "status": "running",
            "completed_at": None,
            "error_type": None,
        }
        path = attempt_dir / "attempt.json"
        _atomic_write_json(path, payload, stem="attempt")
        return path

    def finish_cache_attempt(
        self,
        result_key: str,
        attempt_id: str,
        *,
        status: str,
        error_type: str | None = None,
    ) -> None:
        """Mark a reusable attempt terminal after all possible writers stop."""
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError(f"Invalid cache attempt status: {status!r}")
        if not re.fullmatch(r"att_[0-9a-f]{32}", attempt_id):
            raise ValueError(f"Invalid attempt ID: {attempt_id!r}")
        path = (
            self.result_dir(result_key)
            / "attempts"
            / attempt_id
            / "attempt.json"
        )
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CacheCorruptionError("Cache attempt metadata is unreadable.") from exc
        if (
            payload.get("schema") != "bioimageflow.cache.attempt.v1"
            or payload.get("result_key") != result_key
            or payload.get("attempt_id") != attempt_id
        ):
            raise CacheCorruptionError("Cache attempt metadata correlation mismatch.")
        if payload.get("status") != "running":
            raise CacheCorruptionError("Cache attempt metadata is already terminal.")
        payload["status"] = status
        payload["completed_at"] = datetime.now(timezone.utc).isoformat()
        payload["error_type"] = error_type
        _atomic_write_json(path, payload, stem="attempt")

    def new_invocation_id(self) -> str:
        """Return a non-content processing invocation identifier."""
        return f"inv_{uuid.uuid4().hex}"

    def backend_task_diagnostic_path(
        self,
        run_id: str,
        node_key: str,
        invocation_id: str,
        task_id: str,
    ) -> Path:
        """Return the separate diagnostic path for one backend task."""
        if not re.fullmatch(r"run_[0-9a-f]{32}", run_id):
            raise ValueError(f"Invalid run ID: {run_id!r}")
        if not re.fullmatch(r"inv_[0-9a-f]{32}", invocation_id):
            raise ValueError(f"Invalid invocation ID: {invocation_id!r}")
        if not re.fullmatch(r"task_[0-9a-f]{16}", task_id):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        return (
            self.storage_path
            / "diagnostics"
            / "v1"
            / "runs"
            / run_id
            / "nodes"
            / _validate_node_key(node_key)
            / invocation_id
            / "tasks"
            / f"{task_id}.json"
        )

    def start_backend_task_diagnostic(
        self,
        run_id: str,
        node_key: str,
        invocation_id: str,
        task_id: str,
        *,
        backend: str,
        executor_label: str,
        cache_attempt_id: str | None,
        task_retry: int,
        mode: str,
        row_positions: list[int],
        tool_origin: dict[str, object],
    ) -> Path:
        """Persist submitted backend metadata outside canonical cache state."""
        path = self.backend_task_diagnostic_path(
            run_id,
            node_key,
            invocation_id,
            task_id,
        )
        if not backend or not executor_label:
            raise ValueError("Backend and executor label must be non-empty.")
        if cache_attempt_id is not None and not re.fullmatch(
            r"att_[0-9a-f]{32}",
            cache_attempt_id,
        ):
            raise ValueError(f"Invalid cache attempt ID: {cache_attempt_id!r}")
        if type(task_retry) is not int or task_retry < 0:
            raise ValueError("task_retry must be a non-negative integer.")
        if mode not in {"row_chunk", "process_batch"}:
            raise ValueError(f"Invalid backend task mode: {mode!r}")
        if any(type(position) is not int or position < 0 for position in row_positions):
            raise ValueError("row_positions must contain non-negative integers.")
        self.storage_path.mkdir(parents=True, exist_ok=True)
        storage_root = self.storage_path.resolve()
        parent = self.storage_path
        relative_parent = path.parent.relative_to(self.storage_path)
        for segment in relative_parent.parts:
            candidate = parent / segment
            candidate.mkdir(exist_ok=True)
            if candidate.is_symlink() or not candidate.is_dir():
                raise CacheCorruptionError(
                    "Backend diagnostic path must contain only real directories."
                )
            try:
                candidate.resolve().relative_to(storage_root)
            except ValueError as exc:
                raise CacheCorruptionError(
                    "Backend diagnostic path escapes storage."
                ) from exc
            parent = candidate
        payload = {
            "schema": "bioimageflow.backend_task.v1",
            "backend": backend,
            "run_id": run_id,
            "node_key": node_key,
            "invocation_id": invocation_id,
            "cache_attempt_id": cache_attempt_id,
            "task_id": task_id,
            "executor_label": executor_label,
            "task_retry": task_retry,
            "mode": mode,
            "row_positions": list(row_positions),
            "tool_origin": dict(tool_origin),
            "status": "submitted",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "error_type": None,
        }
        _atomic_write_json(path, payload, stem="backend-task")
        return path

    def finish_backend_task_diagnostic(
        self,
        run_id: str,
        node_key: str,
        invocation_id: str,
        task_id: str,
        *,
        status: str,
        error_type: str | None = None,
    ) -> None:
        """Mark backend task metadata terminal after its future is observed."""
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError(f"Invalid backend task status: {status!r}")
        path = self.backend_task_diagnostic_path(
            run_id,
            node_key,
            invocation_id,
            task_id,
        )
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CacheCorruptionError(
                "Backend task diagnostic is unreadable."
            ) from exc
        expected = {
            "schema": "bioimageflow.backend_task.v1",
            "run_id": run_id,
            "node_key": node_key,
            "invocation_id": invocation_id,
            "task_id": task_id,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise CacheCorruptionError(
                "Backend task diagnostic correlation mismatch."
            )
        if payload.get("status") != "submitted":
            raise CacheCorruptionError(
                "Backend task diagnostic is already terminal."
            )
        payload["status"] = status
        payload["completed_at"] = datetime.now(timezone.utc).isoformat()
        payload["error_type"] = error_type
        _atomic_write_json(path, payload, stem="backend-task")

    def transient_invocation_dir(
        self,
        run_id: str,
        node_key: str,
        invocation_id: str,
    ) -> Path:
        """Return the confined workspace for one non-reusable invocation."""
        if not re.fullmatch(r"run_[0-9a-f]{32}", run_id):
            raise ValueError(f"Invalid run ID: {run_id!r}")
        if not re.fullmatch(r"inv_[0-9a-f]{32}", invocation_id):
            raise ValueError(f"Invalid invocation ID: {invocation_id!r}")
        return (
            self.cache_root
            / "transient"
            / "runs"
            / run_id
            / "nodes"
            / _validate_node_key(node_key)
            / invocation_id
        )

    def create_transient_invocation(
        self,
        run_id: str,
        node_key: str,
        *,
        invocation_id: str | None = None,
        engine: str,
    ) -> tuple[str, Path, Path]:
        """Create a run-scoped non-reusable processing workspace."""
        selected_id = invocation_id or self.new_invocation_id()
        invocation_dir = self.transient_invocation_dir(
            run_id,
            node_key,
            selected_id,
        )
        if invocation_dir.exists() or invocation_dir.is_symlink():
            raise CacheCorruptionError(
                f"Transient invocation already exists: {selected_id}"
            )
        self.storage_path.mkdir(parents=True, exist_ok=True)
        storage_root = self.storage_path.resolve()
        parent = self.storage_path
        node_parts = _validate_node_key(node_key).split("/")
        for segment in (
            "cache",
            "v1",
            "transient",
            "runs",
            run_id,
            "nodes",
            *node_parts,
        ):
            candidate = parent / segment
            if candidate.exists() or candidate.is_symlink():
                if candidate.is_symlink() or not candidate.is_dir():
                    raise CacheCorruptionError(
                        "Transient invocation path must contain only real directories."
                    )
            else:
                candidate.mkdir()
            try:
                candidate.resolve().relative_to(storage_root)
            except ValueError as exc:
                raise CacheCorruptionError(
                    "Transient invocation path escapes the cache root."
                ) from exc
            parent = candidate
        invocation_dir = parent / selected_id
        invocation_dir.mkdir()
        assets_dir = invocation_dir / "assets"
        work_dir = invocation_dir / "work"
        assets_dir.mkdir()
        work_dir.mkdir()
        payload = {
            "schema": "bioimageflow.transient.invocation.v1",
            "run_id": run_id,
            "node_key": node_key,
            "invocation_id": selected_id,
            "engine": engine,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "status": "running",
            "completed_at": None,
        }
        tmp_path = invocation_dir / f".invocation.{uuid.uuid4().hex}.tmp"
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        os.replace(tmp_path, invocation_dir / "invocation.json")
        return selected_id, invocation_dir, assets_dir

    def finish_transient_invocation(
        self,
        run_id: str,
        node_key: str,
        invocation_id: str,
        *,
        status: str,
        error: BaseException | None = None,
    ) -> None:
        """Mark a transient invocation terminal after every writer stops."""
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError(f"Invalid transient invocation status: {status!r}")
        invocation_dir = self.transient_invocation_dir(
            run_id,
            node_key,
            invocation_id,
        )
        metadata_path = invocation_dir / "invocation.json"
        try:
            payload = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CacheCorruptionError(
                "Transient invocation metadata is unreadable."
            ) from exc
        payload["status"] = status
        payload["completed_at"] = datetime.now(timezone.utc).isoformat()
        tmp_path = invocation_dir / f".invocation.{uuid.uuid4().hex}.tmp"
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        os.replace(tmp_path, metadata_path)
        if error is not None:
            failed = {
                "schema": "bioimageflow.transient.failure.v1",
                "type": type(error).__name__,
                "message": str(error),
            }
            tmp_failure = invocation_dir / f".failed.{uuid.uuid4().hex}.tmp"
            tmp_failure.write_text(json.dumps(failed, indent=2, sort_keys=True))
            os.replace(tmp_failure, invocation_dir / "failed.json")

    def load_current(self, result_key: str) -> CurrentPointer | None:
        result_dir = self.result_dir(result_key)
        path = result_dir / "current.json"
        if not path.exists():
            return None
        try:
            pointer = CurrentPointer.from_dict(json.loads(path.read_text()))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise CacheCorruptionError(
                f"Invalid current pointer for {result_key}"
            ) from exc
        if pointer.result_key != result_key:
            raise CacheCorruptionError("Current pointer result key mismatch.")
        try:
            manifest_path = result_dir / validate_relative_posix_path(pointer.manifest)
        except ValueError as exc:
            raise CacheCorruptionError(
                "Current pointer manifest path is unsafe."
            ) from exc
        try:
            manifest_path.resolve().relative_to(result_dir.resolve())
        except ValueError as exc:
            raise CacheCorruptionError(
                "Current pointer manifest escapes result directory."
            ) from exc
        if not manifest_path.exists():
            raise CacheCorruptionError("Current pointer references a missing manifest.")
        manifest = self._load_record_manifest(result_key, pointer.record_id)
        if manifest.record_id != pointer.record_id:
            raise CacheCorruptionError("Current pointer record ID mismatch.")
        return pointer

    def select_current_record(
        self,
        result_key: str,
        *,
        candidate_record_id: str,
        attempt_id: str,
        run_id: str,
    ) -> CurrentPointer:
        result_dir = self.result_dir(result_key)
        records_dir = result_dir / "records"
        candidate_record_id = _validate_record_id(candidate_record_id)
        self._load_record_manifest(result_key, candidate_record_id)
        candidate_manifest = records_dir / candidate_record_id / "manifest.json"
        if not candidate_manifest.exists():
            raise CacheCorruptionError("Candidate record manifest is missing.")
        result_dir.mkdir(parents=True, exist_ok=True)
        pointer = CurrentPointer(
            result_key=result_key,
            record_id=candidate_record_id,
            manifest=f"records/{candidate_record_id}/manifest.json",
            attempt_id=attempt_id,
            run_id=run_id,
        )
        current_path = result_dir / "current.json"
        candidate_path = result_dir / f".current.{uuid.uuid4().hex}.candidate"
        candidate_path.write_text(
            json.dumps(pointer.to_dict(), indent=2, sort_keys=True)
        )
        try:
            try:
                os.link(candidate_path, current_path)
                return pointer
            except FileExistsError:
                existing = self.load_current(result_key)
                if existing is None:
                    raise CacheCorruptionError(
                        "Current pointer disappeared during guarded selection."
                    )
                if existing.record_id != candidate_record_id:
                    self._write_conflict(
                        result_key,
                        existing.record_id,
                        candidate_record_id,
                        attempt_id,
                        run_id,
                    )
                return existing
        finally:
            candidate_path.unlink(missing_ok=True)

    def _load_record_manifest(self, result_key: str, record_id: str) -> RecordManifest:
        record_id = _validate_record_id(record_id)
        result_dir = self.result_dir(result_key)
        records_dir = result_dir / "records"
        record_dir = records_dir / record_id
        try:
            record_dir.resolve().relative_to(records_dir.resolve())
        except ValueError as exc:
            raise CacheCorruptionError(
                "Record directory escapes records directory."
            ) from exc
        if record_dir.is_symlink():
            raise CacheCorruptionError("Record directory must not be a symlink.")
        manifest_path = record_dir / "manifest.json"
        if not manifest_path.exists():
            raise CacheCorruptionError("Record manifest is missing.")
        try:
            manifest_path.resolve().relative_to(record_dir.resolve())
        except ValueError as exc:
            raise CacheCorruptionError(
                "Record manifest escapes record directory."
            ) from exc
        if manifest_path.is_symlink():
            raise CacheCorruptionError("Record manifest must not be a symlink.")
        try:
            manifest = RecordManifest.from_dict(json.loads(manifest_path.read_text()))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CacheCorruptionError("Invalid record manifest JSON.") from exc
        manifest.validate(record_dir, expected_result_key=result_key)
        return manifest

    def _write_conflict(
        self,
        result_key: str,
        current_record_id: str,
        candidate_record_id: str,
        attempt_id: str,
        run_id: str,
    ) -> None:
        conflicts_dir = self.result_dir(result_key) / "conflicts"
        conflicts_dir.mkdir(exist_ok=True)
        conflict_id = _sha256_token(
            "conflict",
            {
                "result_key": result_key,
                "current_record_id": current_record_id,
                "candidate_record_id": candidate_record_id,
                "attempt_id": attempt_id,
                "run_id": run_id,
            },
        )
        payload = {
            "schema": "bioimageflow.cache.conflict.v1",
            "result_key": result_key,
            "current_record_id": current_record_id,
            "candidate_record_id": candidate_record_id,
            "attempt_id": attempt_id,
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write_json(
            conflicts_dir / f"{conflict_id}.json",
            payload,
            stem="conflict",
        )
