"""Deterministic composition harness for the remote cluster boundary."""

from __future__ import annotations

import json
import shlex
import shutil
from datetime import timedelta
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bioimageflow.launcher.cluster_agent import run_agent
from bioimageflow.launcher.cluster_protocol import request
from bioimageflow.launcher.orchestrator import run_orchestrator
from bioimageflow.launcher.repository import LauncherRepository
from bioimageflow.launcher.ssh import SSHTransportError, _decode_response
from bioimageflow.storage import canonical_json_bytes


class _JobState(Enum):
    NEW = "NEW"
    QUEUED = "QUEUED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class _Status:
    def __init__(self, state: _JobState) -> None:
        self.state = state


class _Record:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


class FakeCluster:
    """Compose real bundle, agent, launcher, Parsl, and result code in one process."""

    def __init__(self, *, lose_submit_response: bool = False) -> None:
        self.control: Any | None = None
        self.launch_count = 0
        self.job_specs: list[Any] = []
        self.job_states: dict[str, _JobState] = {}
        self.operation_counts: dict[str, int] = {}
        self._lose_submit_response = lose_submit_response
        self._submit_response_lost = False

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
            "bioimageflow.launcher.psij._load_runtime",
            self._runtime,
        )
        monkeypatch.setattr(
            "bioimageflow.launcher.result_download._run_sftp",
            self.download,
        )

    def _runtime(self) -> Any:
        cluster = self

        class Job:
            def __init__(self, spec: Any = None) -> None:
                self.spec = spec
                self.native_id: str | None = None
                self.status = _Status(_JobState.NEW)

            def wait(
                self,
                timeout: timedelta | None = None,
                target_states: Any = None,
            ) -> _Status:
                del timeout, target_states
                if self.native_id is not None:
                    self.status = _Status(cluster.job_states[self.native_id])
                return self.status

            def cancel(self) -> None:
                if self.native_id is None:
                    raise AssertionError("Cannot cancel an unattached fake job.")
                cluster.job_states[self.native_id] = _JobState.CANCELED
                self.status = _Status(_JobState.CANCELED)

        class Executor:
            def __init__(self, name: str, config: Any) -> None:
                self.name = name
                self.config = config

            def submit(self, job: Job) -> None:
                cluster.launch_count += 1
                if cluster.launch_count != 1:
                    raise AssertionError(
                        "The fake PSI/J boundary received a duplicate orchestrator job."
                    )
                native_id = "fake-native-1"
                job.native_id = native_id
                job.status = _Status(_JobState.QUEUED)
                cluster.job_states[native_id] = _JobState.QUEUED
                cluster.job_specs.append(job.spec)
                arguments = list(job.spec.arguments)
                storage_index = arguments.index("--storage-root") + 1
                run_index = arguments.index("--run-id") + 1
                cluster.control = LauncherRepository(
                    Path(arguments[storage_index])
                ).open(arguments[run_index])

            def attach(self, job: Job, native_id: str) -> None:
                job.native_id = native_id
                job.status = _Status(cluster.job_states[native_id])

        class JobExecutor:
            @classmethod
            def get_executor_names(cls) -> set[str]:
                return {"slurm", "pbs", "lsf"}

            @classmethod
            def get_instance(
                cls,
                name: str,
                config: Any = None,
            ) -> Executor:
                return Executor(name, config)

        return SimpleNamespace(
            Job=Job,
            JobState=_JobState,
            JobSpec=_Record,
            JobAttributes=_Record,
            ResourceSpecV1=_Record,
            JobExecutor=JobExecutor,
            JobExecutorConfig=_Record,
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
        self.operation_counts[operation] = self.operation_counts.get(operation, 0) + 1
        encoded = canonical_json_bytes(
            request(operation, arguments, request_id=request_id)
        )
        response = run_agent(encoded)
        if (
            operation == "submit"
            and self._lose_submit_response
            and not self._submit_response_lost
        ):
            self._submit_response_lost = True
            raise SSHTransportError(
                "ssh-connection",
                "Injected response loss after cluster acceptance.",
                ambiguous=True,
            )
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

    def run_queued_job(self) -> str:
        """Execute the queued orchestrator through the real local Parsl runtime."""
        if self.control is None:
            raise AssertionError("No fake scheduler job is queued.")
        self.job_states["fake-native-1"] = _JobState.ACTIVE
        outcome = run_orchestrator(
            self.control.repository.storage_root,
            self.control.run_id,
            lease_seconds=5,
            poll_seconds=0.01,
        )
        self.job_states["fake-native-1"] = (
            _JobState.COMPLETED if outcome == "succeeded" else _JobState.FAILED
        )
        return outcome

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
