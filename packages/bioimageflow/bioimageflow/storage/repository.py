"""Focused storage repository behavior."""

from __future__ import annotations

from .common import (
    Path,
    datetime,
    json,
    os,
    timezone,
    uuid,
)
from .models import (
    CacheCorruptionError,
)
from .identity import (
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
        return f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}_{uuid.uuid4().hex[:12]}"

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
        lock_dir = result_dir / ".current.lock"
        while True:
            try:
                lock_dir.mkdir()
                break
            except FileExistsError:
                # Local primitive only: fail fast instead of spinning forever.
                raise CacheCorruptionError(
                    "Current pointer is locked by another writer."
                )
        try:
            existing = self.load_current(result_key)
            if existing is not None:
                if existing.record_id != candidate_record_id:
                    self._write_conflict(
                        result_key,
                        existing.record_id,
                        candidate_record_id,
                        attempt_id,
                        run_id,
                    )
                return existing
            pointer = CurrentPointer(
                result_key=result_key,
                record_id=candidate_record_id,
                manifest=f"records/{candidate_record_id}/manifest.json",
                attempt_id=attempt_id,
                run_id=run_id,
            )
            tmp_path = result_dir / f".current.{uuid.uuid4().hex}.tmp"
            tmp_path.write_text(json.dumps(pointer.to_dict(), indent=2, sort_keys=True))
            os.replace(tmp_path, result_dir / "current.json")
            return pointer
        finally:
            lock_dir.rmdir()

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
        manifest_path = record_dir / "manifest.json"
        if not manifest_path.exists():
            raise CacheCorruptionError("Record manifest is missing.")
        try:
            manifest_path.resolve().relative_to(record_dir.resolve())
        except ValueError as exc:
            raise CacheCorruptionError(
                "Record manifest escapes record directory."
            ) from exc
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
        (conflicts_dir / f"{conflict_id}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True)
        )
