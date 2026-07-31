"""Transport-backed public façade for one cluster-local WorkflowRun."""

from __future__ import annotations

import base64
import math
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from bioimageflow.engine import WorkflowCancelledError

from .errors import (
    WorkflowRunFailedError,
    WorkflowRunLostError,
    WorkflowRunNotReadyError,
    WorkflowRunResultUnavailableError,
)
from .schemas import validate_run_id
from .types import SSHSubmissionTransport

_PROGRESS_PAGE = 500
_LOG_PAGE = 256 * 1024


def _cluster_path(value: PurePosixPath | str) -> PurePosixPath:
    if not isinstance(value, (str, PurePosixPath)) or not str(value):
        raise TypeError("storage_path must be an absolute cluster POSIX path.")
    text = str(value)
    path = PurePosixPath(text)
    if (
        not path.is_absolute()
        or text.startswith("//")
        or str(path) != text
        or any(part in {"", ".", ".."} for part in path.parts[1:])
        or any(character in text for character in ("\x00", "\n", "\r"))
    ):
        raise ValueError("storage_path must be a normalized absolute cluster POSIX path.")
    return path


class RemoteWorkflowRun:
    """Remote control handle with no local launcher control-path claims."""

    def __init__(
        self,
        transport: SSHSubmissionTransport,
        storage_path: PurePosixPath | str,
        run_id: str,
        *,
        observation: Mapping[str, Any],
    ) -> None:
        if type(transport) is not SSHSubmissionTransport:
            raise TypeError("transport must be an SSHSubmissionTransport.")
        self._transport = transport
        self._storage_path = _cluster_path(storage_path)
        self.id = validate_run_id(run_id)
        self._apply_observation(observation)

    @classmethod
    def open(
        cls,
        transport: SSHSubmissionTransport,
        storage_path: PurePosixPath | str,
        run_id: str,
    ) -> "RemoteWorkflowRun":
        """Reconnect from only transport, cluster storage path, and run ID."""
        if type(transport) is not SSHSubmissionTransport:
            raise TypeError("transport must be an SSHSubmissionTransport.")
        storage = _cluster_path(storage_path)
        canonical_run_id = validate_run_id(run_id)
        from .ssh import execute_cluster_command

        observation = execute_cluster_command(
            transport,
            "inspect",
            {
                "run_id": canonical_run_id,
                "storage_path": str(storage),
            },
            request_id=str(uuid.uuid4()),
        )
        return cls(
            transport,
            storage,
            canonical_run_id,
            observation=observation,
        )

    @classmethod
    def _submitted(
        cls,
        transport: SSHSubmissionTransport,
        storage_path: PurePosixPath | str,
        run_id: str,
    ) -> "RemoteWorkflowRun":
        return cls.open(transport, storage_path, run_id)

    @property
    def status(self) -> str:
        """Return the last authoritative state received from the cluster."""
        return self._status

    def _arguments(self) -> dict[str, str]:
        return {
            "run_id": self.id,
            "storage_path": str(self._storage_path),
        }

    def _apply_observation(self, observation: Mapping[str, Any]) -> None:
        if not isinstance(observation, Mapping):
            raise TypeError("observation must be a remote observation mapping.")
        from .ssh import _validate_observation

        _validate_observation(observation, self._arguments())
        if (
            observation.get("run_id") != self.id
            or observation.get("storage_path") != str(self._storage_path)
        ):
            raise RuntimeError("Remote observation changed the run binding.")
        self._status = str(observation["state"])
        error = observation.get("error")
        self._error = dict(error) if isinstance(error, dict) else None

    def refresh(self) -> None:
        """Refresh through the cluster-local WorkflowRun reconciliation path."""
        from .ssh import execute_cluster_command

        observation = execute_cluster_command(
            self._transport,
            "refresh",
            self._arguments(),
            request_id=str(uuid.uuid4()),
        )
        self._apply_observation(observation)

    def wait(
        self,
        *,
        timeout: float | None = None,
        poll_interval: float = 2.0,
    ) -> str:
        """Poll durable state without starting a persistent server process."""
        if (
            type(poll_interval) not in {int, float}
            or not math.isfinite(float(poll_interval))
            or not 0 < float(poll_interval) <= 3600
        ):
            raise ValueError("poll_interval must be finite and in (0, 3600].")
        if timeout is not None and (
            type(timeout) not in {int, float}
            or not math.isfinite(float(timeout))
            or float(timeout) < 0
        ):
            raise ValueError("timeout must be a finite non-negative number or None.")
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            self.refresh()
            if self.status in {"succeeded", "failed", "cancelled", "lost"}:
                return self.status
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            if remaining == 0:
                raise TimeoutError(f"Workflow run {self.id} did not become terminal.")
            delay = float(poll_interval) if remaining is None else min(
                float(poll_interval),
                remaining,
            )
            threading.Event().wait(delay)

    def progress(
        self,
        *,
        after_sequence: int = 0,
    ) -> list[dict[str, Any]]:
        """Read all currently available bounded pages after a global sequence."""
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer.")
        cursor = after_sequence
        events: list[dict[str, Any]] = []
        from .ssh import execute_cluster_command

        while True:
            result = execute_cluster_command(
                self._transport,
                "read-progress",
                {
                    **self._arguments(),
                    "after_sequence": cursor,
                    "limit": _PROGRESS_PAGE,
                },
                request_id=str(uuid.uuid4()),
            )
            self._apply_observation(result)
            events.extend(result["events"])
            next_cursor = result["next_sequence"]
            if not result["has_more"]:
                return events
            if next_cursor <= cursor:
                raise RuntimeError("Remote progress pagination did not advance.")
            cursor = next_cursor

    def diagnostics(self) -> tuple[Any, ...]:
        """Return structured node failures through the public remote protocol."""
        from bioimageflow.integration import NodeFailureDiagnostic

        return tuple(
            NodeFailureDiagnostic.from_dict(event["payload"])
            for event in self.progress()
            if event["kind"] == "diagnostic"
        )

    def _read_log(self, stream: str) -> bytes | None:
        from .ssh import execute_cluster_command

        identity: str | None = None
        snapshot_size: int | None = None
        offset = 0
        content = bytearray()
        resets = 0
        while True:
            result = execute_cluster_command(
                self._transport,
                "read-logs",
                {
                    **self._arguments(),
                    "identity": identity,
                    "limit": _LOG_PAGE,
                    "offset": offset,
                    "snapshot_size": snapshot_size,
                    "stream": stream,
                },
                request_id=str(uuid.uuid4()),
            )
            self._apply_observation(result)
            if not result["exists"]:
                return None
            if result["reset"]:
                resets += 1
                if resets > 3:
                    raise RuntimeError("Remote log changed repeatedly during reading.")
                content.clear()
                offset = 0
            identity = result["identity"]
            snapshot_size = result["snapshot_size"]
            content.extend(base64.b64decode(result["data"], validate=True))
            offset = result["next_offset"]
            if result["eof"]:
                return bytes(content)

    def logs(self) -> str:
        """Return the same combined replacement-decoded text as local logs()."""
        sections: list[str] = []
        for stream in ("stdout", "stderr"):
            content = self._read_log(stream)
            if content is not None:
                sections.append(
                    f"[{stream}]\n{content.decode('utf-8', errors='replace')}"
                )
        return "\n".join(sections)

    def cancel(self) -> None:
        """Request retry-safe cancellation through cluster-local WorkflowRun."""
        from .ssh import _retry_mutation

        result = _retry_mutation(
            self._transport,
            "cancel",
            {
                **self._arguments(),
                "staging_root": str(self._transport.staging_root),
            },
            str(uuid.uuid4()),
        )
        self._apply_observation(result)

    def _raise_result_state(self) -> None:
        if self.status == "failed":
            error = self._error or {
                "code": "workflow-execution-failed",
                "message": f"Submitted workflow run {self.id} failed.",
            }
            raise WorkflowRunFailedError(error["message"], error=error)
        if self.status == "cancelled":
            raise WorkflowCancelledError(
                f"Submitted workflow run {self.id} was cancelled."
            )
        if self.status == "lost":
            raise WorkflowRunLostError(
                f"Submitted workflow run {self.id} was lost.",
                details=self._error or {"run_id": self.id},
            )
        raise WorkflowRunNotReadyError(
            f"Submitted workflow run {self.id} is {self.status!r}, not succeeded.",
            details={"run_id": self.id, "state": self.status},
        )

    def result(self, *, destination: Path | str) -> Any:
        """Atomically download and rehydrate the successful typed return."""
        if not isinstance(destination, (str, Path)) or not str(destination):
            raise TypeError("destination must be a non-empty local path.")
        self.refresh()
        if self.status != "succeeded":
            self._raise_result_state()
        from .result_download import download_result
        from .ssh import SSHTransportError, _retry_mutation

        try:
            response = _retry_mutation(
                self._transport,
                "prepare-result",
                {
                    **self._arguments(),
                    "staging_root": str(self._transport.staging_root),
                },
                str(uuid.uuid4()),
            )
        except SSHTransportError as exc:
            if exc.code == "remote-result-unavailable":
                raise WorkflowRunResultUnavailableError(
                    "The successful remote workflow return is unavailable.",
                    details={"run_id": self.id},
                ) from exc
            raise
        return download_result(
            self._transport,
            response,
            Path(destination),
        )
