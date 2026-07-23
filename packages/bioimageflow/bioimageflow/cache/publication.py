"""Atomic installation of complete immutable cache records."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from bioimageflow.storage import CacheCorruptionError, RecordManifest, Storage

from .processing_lookup import _ensure_records_dir


def create_record_candidate(
    storage: Storage,
    result_key: str,
    attempt_id: str,
    record_id: str,
) -> Path:
    """Create a private sibling directory for one complete record candidate."""
    records_dir = _ensure_records_dir(storage.result_dir(result_key))
    candidate_root = (
        records_dir / f".candidate.{attempt_id}.{uuid.uuid4().hex}"
    )
    candidate_root.mkdir()
    candidate = candidate_root / record_id
    candidate.mkdir()
    return candidate


def install_record_candidate(
    storage: Storage,
    result_key: str,
    record_id: str,
    candidate: Path,
) -> Path:
    """Validate and atomically install a candidate, or use a concurrent winner."""
    candidate_root = candidate.parent
    final = candidate_root.parent / record_id
    try:
        manifest = RecordManifest.from_dict(
            json.loads((candidate / "manifest.json").read_text())
        )
        if manifest.record_id != record_id:
            raise CacheCorruptionError("Candidate record ID mismatch.")
        manifest.validate(candidate, expected_result_key=result_key)
        try:
            candidate.rename(final)
        except OSError as exc:
            if final.is_symlink():
                raise CacheCorruptionError(
                    "Installed record must not be a symlink."
                ) from exc
            if not final.is_dir():
                raise
            storage._load_record_manifest(result_key, record_id)
            shutil.rmtree(candidate_root)
        else:
            candidate_root.rmdir()
        storage._load_record_manifest(result_key, record_id)
        return final
    except BaseException:
        if candidate_root.exists() and not candidate_root.is_symlink():
            shutil.rmtree(candidate_root)
        raise


__all__ = ["create_record_candidate", "install_record_candidate"]
