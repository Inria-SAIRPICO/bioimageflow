"""Deterministic composition harness for the remote cluster boundary."""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path
from typing import Any

from bioimageflow.launcher.cluster_agent import run_agent
from bioimageflow.launcher.cluster_protocol import request
from bioimageflow.launcher.orchestrator import run_orchestrator
from bioimageflow.launcher.ssh import _decode_response
from bioimageflow.storage import canonical_json_bytes


class FakeCluster:
    """Compose real bundle, agent, launcher, Parsl, and result code in one process."""

    def __init__(self) -> None:
        self.control: Any | None = None
        self.launch_count = 0

    def install(self, monkeypatch: Any) -> None:
        """Install only transport and scheduler process-boundary fakes."""
        monkeypatch.setattr(
            "bioimageflow.launcher.ssh.execute_cluster_command",
            self.execute,
        )
        monkeypatch.setattr(
            "bioimageflow.launcher.ssh.upload_bundle",
            self.upload,
        )
        monkeypatch.setattr(
            "bioimageflow.launcher.backends.launch_orchestrator",
            self.launch,
        )
        monkeypatch.setattr(
            "bioimageflow.launcher.result_download._run_sftp",
            self.download,
        )

    def execute(
        self,
        transport: Any,
        operation: str,
        arguments: dict[str, Any],
        *,
        request_id: str,
    ) -> dict[str, Any]:
        """Run the installed cluster agent and the production response decoder."""
        encoded = canonical_json_bytes(
            request(operation, arguments, request_id=request_id)
        )
        response = run_agent(encoded)
        return _decode_response(
            response,
            request_id,
            operation,
            transport,
            arguments,
        )

    def upload(self, transport: Any, bundle: Any, remote_root: str) -> None:
        """Stand in only for SFTP bytes; server validation remains production code."""
        del transport
        shutil.copytree(bundle.root, Path(remote_root), dirs_exist_ok=True)

    def launch(self, control: Any, launch: Any, *, secret_refs: Any) -> None:
        """Record one fake scheduler job and leave it queued until explicitly run."""
        del launch, secret_refs
        self.launch_count += 1
        if self.control is not None:
            raise AssertionError("The fake cluster received a duplicate orchestrator job.")
        self.control = control
        control.append_progress(
            kind="backend",
            payload={
                "schema": "bioimageflow.launcher.backend_event.v1",
                "event": "psij_queued",
                "executor": "slurm",
                "native_id": "fake-native-1",
                "state": "QUEUED",
                "message": None,
            },
        )

    def run_queued_job(self) -> str:
        """Execute the queued orchestrator through the real local Parsl runtime."""
        if self.control is None:
            raise AssertionError("No fake scheduler job is queued.")
        return run_orchestrator(
            self.control.repository.storage_root,
            self.control.run_id,
            lease_seconds=5,
            poll_seconds=0.01,
        )

    def download(self, transport: Any, commands: list[str]) -> None:
        """Stand in only for SFTP get operations."""
        del transport
        for command in commands:
            words = shlex.split(command)
            if len(words) != 3 or words[0] != "get":
                raise AssertionError(f"Unexpected fake SFTP command: {command!r}")
            source = Path(words[1])
            destination = Path(words[2])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    def request_log(self, staging_root: Path) -> list[dict[str, Any]]:
        """Read operation receipts for integration assertions."""
        values = []
        for path in sorted((staging_root / "receipts").rglob("*.json")):
            values.append(json.loads(path.read_text(encoding="utf-8")))
        return values
