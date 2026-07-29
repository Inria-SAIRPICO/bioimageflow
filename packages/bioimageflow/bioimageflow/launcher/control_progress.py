"""Guarded append-only launcher progress operations."""

# Pyright checks the complete contract on LauncherRunControl.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from .repository import (
    ClaimConflictError,
    LauncherCorruptionError,
    LauncherStorageError,
    _canonical_json_bytes,
    _decode_json,
    _open_binary_no_follow,
    _read_bytes,
)
from .schemas import (
    PROGRESS_SCHEMA,
    LauncherSchemaError,
    utc_timestamp,
    validate_progress,
)
from .state import ClaimEpochMismatchError


class _ProgressControlMixin:
    def append_progress(
        self,
        *,
        kind: str,
        payload: Mapping[str, Any],
        timestamp: str | None = None,
        expected_claim_epoch: int | None = None,
        expected_claim_nonce: str | None = None,
        allow_terminal_claim: bool = False,
    ) -> dict[str, Any]:
        """Allocate and durably append the next global progress sequence."""
        with self.guard():
            if (
                expected_claim_epoch is None
            ) != (expected_claim_nonce is None):
                raise ValueError(
                    "Progress claim epoch and nonce must be supplied together."
                )
            if expected_claim_epoch is not None:
                status = self._read_status_unlocked()
                if (
                    status["state"]
                    in {"succeeded", "failed", "cancelled", "lost"}
                    and not allow_terminal_claim
                ):
                    raise ClaimConflictError(
                        "Terminal launcher status rejects further claimed progress."
                    )
                if status["claim_epoch"] != expected_claim_epoch:
                    raise ClaimEpochMismatchError(
                        "Progress writer no longer owns the status claim epoch."
                    )
                claim = self._require_active_claim_epoch(
                    expected_claim_epoch
                )
                if claim["nonce"] != expected_claim_nonce:
                    raise ClaimConflictError(
                        "Progress writer no longer owns the execution claim."
                    )
            entries, complete_length = self._read_progress_unlocked()
            sequence = entries[-1]["sequence"] + 1 if entries else 1
            entry = validate_progress(
                {
                    "schema": PROGRESS_SCHEMA,
                    "run_id": self.run_id,
                    "sequence": sequence,
                    "timestamp": timestamp or utc_timestamp(),
                    "kind": kind,
                    "payload": dict(payload),
                }
            )
            encoded = _canonical_json_bytes(entry) + b"\n"
            self.confined_path("progress.jsonl", must_exist=True)
            flags = os.O_RDWR
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.progress_path, flags)
            try:
                os.ftruncate(descriptor, complete_length)
                os.lseek(descriptor, 0, os.SEEK_END)
                written = os.write(descriptor, encoded)
                if written != len(encoded):
                    raise LauncherStorageError(
                        "Short launcher progress write."
                    )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return entry

    def read_progress(self) -> list[dict[str, Any]]:
        """Read complete progress entries, ignoring one incomplete tail."""
        self.confined_path("progress.jsonl", must_exist=True)
        entries, _ = self._read_progress_unlocked()
        return entries

    def read_progress_page(
        self,
        *,
        after_sequence: int,
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Read one page without retaining the complete progress history."""
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer.")
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer.")
        self.confined_path("progress.jsonl", must_exist=True)
        page: list[dict[str, Any]] = []
        previous = 0
        with _open_binary_no_follow(self.progress_path) as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.endswith(b"\n"):
                    break
                if line == b"\n":
                    raise LauncherCorruptionError(
                        f"Blank complete progress line {line_number}."
                    )
                try:
                    entry = validate_progress(
                        _decode_json(line[:-1], path=self.progress_path)
                    )
                except LauncherSchemaError as error:
                    raise LauncherCorruptionError(
                        f"Invalid progress entry on line {line_number}."
                    ) from error
                if entry["run_id"] != self.run_id:
                    raise LauncherCorruptionError(
                        f"Progress line {line_number} has the wrong run ID."
                    )
                if entry["sequence"] != previous + 1:
                    raise LauncherCorruptionError(
                        f"Progress line {line_number} has non-monotonic sequence."
                    )
                previous = entry["sequence"]
                if previous <= after_sequence:
                    continue
                if len(page) == limit:
                    return page, True
                page.append(entry)
        return page, False

    def _read_progress_unlocked(self) -> tuple[list[dict[str, Any]], int]:
        encoded = _read_bytes(self.progress_path)
        last_newline = encoded.rfind(b"\n")
        complete_length = last_newline + 1
        complete = encoded[:complete_length]
        entries: list[dict[str, Any]] = []
        for line_number, line in enumerate(complete.splitlines(), start=1):
            if not line:
                raise LauncherCorruptionError(
                    f"Blank complete progress line {line_number}."
                )
            try:
                entry = validate_progress(
                    _decode_json(line, path=self.progress_path)
                )
            except LauncherSchemaError as error:
                raise LauncherCorruptionError(
                    f"Invalid progress entry on line {line_number}."
                ) from error
            if entry["run_id"] != self.run_id:
                raise LauncherCorruptionError(
                    f"Progress line {line_number} has the wrong run ID."
                )
            expected = entries[-1]["sequence"] + 1 if entries else 1
            if entry["sequence"] != expected:
                raise LauncherCorruptionError(
                    f"Progress line {line_number} has non-monotonic sequence."
                )
            entries.append(entry)
        return entries, complete_length
