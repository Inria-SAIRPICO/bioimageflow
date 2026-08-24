"""Bounded bootstrap transport used before a stable cluster gateway exists."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from ._common import normalized_cluster_path, validate_host
from .transport import GatewayTransportError, _environment, _timeout_option


BOOTSTRAP_SCHEMA = "bioimageflow.cluster.bootstrap.v1"
BOOTSTRAP_MAX_RESPONSE_BYTES = 64 * 1024
_SAFE_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9._/+:@-]+$")


def _reject_response_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate bootstrap response key")
        result[key] = value
    return result

# This command and program are library-owned constants.  All variable values arrive
# over stdin and every consumed value is validated before it is used as a quoted
# operand.  The candidate is a self-contained, locally supplied Python artifact;
# the bootstrap never fetches executable control-plane code.
_BOOTSTRAP_PROGRAM = r'''set -euo pipefail
umask 077
IFS= read -r magic
IFS= read -r stage
IFS= read -r cluster_root
IFS= read -r operation_id
IFS= read -r request_digest
IFS= read -r setup_size
IFS= read -r setup_digest
IFS= read -r artifact_size
IFS= read -r artifact_digest
[[ "$magic" == "bioimageflow.cluster.bootstrap.v1" ]]
[[ "$stage" == "probe" || "$stage" == "allocate" || "$stage" == "publish" ]]
[[ "$cluster_root" =~ ^/[A-Za-z0-9._/+:@-]+$ ]]
[[ "$cluster_root" != *"//"* && "$cluster_root" != *"/./"* && "$cluster_root" != *"/../"* ]]
[[ "$operation_id" =~ ^[0-9a-f-]{36}$ ]]
[[ "$request_digest" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$setup_size" =~ ^[0-9]+$ && "$artifact_size" =~ ^[0-9]+$ ]]
[[ "$setup_digest" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$artifact_digest" =~ ^sha256:[0-9a-f]{64}$ ]]
if [[ "$stage" == "probe" ]]; then
    if [[ -x "$cluster_root/gateway/entry" ]]; then
        printf '%s\n' '{"schema":"bioimageflow.cluster.bootstrap.v1","status":"gateway-ready","upload_path":null}'
    else
        printf '%s\n' '{"schema":"bioimageflow.cluster.bootstrap.v1","status":"bootstrap-required","upload_path":null}'
    fi
    exit 0
fi
private_directory() {
    local checked="$1"
    local mode
    [[ -d "$checked" && ! -L "$checked" && -O "$checked" ]]
    mode="$(stat -c %a -- "$checked")"
    (( (8#$mode & 8#22) == 0 ))
}
if [[ ! -e "$cluster_root" ]]; then
    mkdir -m 700 -- "$cluster_root"
fi
private_directory "$cluster_root"
for namespace in gateway deployments objects operations runs transfers results temporary; do
    path="$cluster_root/$namespace"
    if [[ ! -e "$path" ]]; then mkdir -m 700 -- "$path"; fi
    private_directory "$path"
done
candidate="$cluster_root/temporary/bootstrap-$operation_id"
setup_path="$candidate/setup.sh"
artifact_path="$candidate/gateway.pyz"
receipt="$cluster_root/operations/bootstrap-$operation_id-$stage.receipt"
if [[ -e "$receipt" ]]; then
    [[ -f "$receipt" && ! -L "$receipt" && -O "$receipt" && "$(stat -c %h -- "$receipt")" -eq 1 ]]
    { IFS= read -r recorded_digest; IFS= read -r recorded_phase; } < "$receipt"
    [[ "$recorded_digest" == "$request_digest" && ( "$recorded_phase" == "intent" || "$recorded_phase" == "completed" ) ]]
else
    if [[ "$stage" == "allocate" && -e "$candidate" ]]; then exit 73; fi
    receipt_candidate="$cluster_root/operations/.bootstrap-$operation_id-$stage.partial"
    ( set -C; printf '%s\n%s\n' "$request_digest" intent > "$receipt_candidate" )
    chmod 600 -- "$receipt_candidate"
    mv -n -- "$receipt_candidate" "$receipt"
    [[ -f "$receipt" && ! -L "$receipt" && -O "$receipt" ]]
    { IFS= read -r recorded_digest; IFS= read -r recorded_phase; } < "$receipt"
    [[ "$recorded_digest" == "$request_digest" && "$recorded_phase" == "intent" ]]
    sync -f "$cluster_root/operations"
fi
if [[ "$stage" == "allocate" ]]; then
    if [[ ! -e "$candidate" ]]; then mkdir -m 700 -- "$candidate"; fi
    private_directory "$candidate"
    if [[ "$setup_size" -gt 65536 ]]; then exit 65; fi
    dd bs=1 count="$setup_size" of="$setup_path" status=none
    [[ ! -L "$setup_path" && -f "$setup_path" && -O "$setup_path" && "$(stat -c %h -- "$setup_path")" -eq 1 ]]
    actual="sha256:$(sha256sum -- "$setup_path" | cut -d ' ' -f 1)"
    [[ "$actual" == "$setup_digest" ]]
    chmod 600 -- "$setup_path"
    receipt_candidate="$cluster_root/operations/.bootstrap-$operation_id-$stage.complete"
    printf '%s\n%s\n' "$request_digest" completed > "$receipt_candidate"
    chmod 600 -- "$receipt_candidate"
    mv -f -- "$receipt_candidate" "$receipt"
    sync -f "$cluster_root/operations"
    printf '{"schema":"bioimageflow.cluster.bootstrap.v1","status":"upload-authorized","upload_path":"%s"}\n' "$artifact_path"
    exit 0
fi
private_directory "$candidate"
[[ -f "$setup_path" && ! -L "$setup_path" && -O "$setup_path" && "$(stat -c %h -- "$setup_path")" -eq 1 ]]
[[ -f "$artifact_path" && ! -L "$artifact_path" && -O "$artifact_path" && "$(stat -c %h -- "$artifact_path")" -eq 1 ]]
chmod 600 -- "$artifact_path"
[[ "$(wc -c < "$artifact_path")" -eq "$artifact_size" ]]
actual="sha256:$(sha256sum -- "$artifact_path" | cut -d ' ' -f 1)"
[[ "$actual" == "$artifact_digest" ]]
actual="sha256:$(sha256sum -- "$setup_path" | cut -d ' ' -f 1)"
[[ "$actual" == "$setup_digest" ]]
source "$setup_path"
python_path="$(command -v python3 || command -v python)"
[[ "$python_path" == /* && -x "$python_path" ]]
"$python_path" "$artifact_path" --install-root "$cluster_root" --operation-id "$operation_id"
[[ -x "$cluster_root/gateway/entry" && ! -L "$cluster_root/gateway/entry" ]]
receipt_candidate="$cluster_root/operations/.bootstrap-$operation_id-$stage.complete"
printf '%s\n%s\n' "$request_digest" completed > "$receipt_candidate"
chmod 600 -- "$receipt_candidate"
mv -f -- "$receipt_candidate" "$receipt"
sync -f "$cluster_root/operations"
printf '%s\n' '{"schema":"bioimageflow.cluster.bootstrap.v1","status":"gateway-ready","upload_path":null}'
'''
BOOTSTRAP_REMOTE_COMMAND = (
    "bash --noprofile --norc -c "
    + "'"
    + _BOOTSTRAP_PROGRAM.replace("'", "'\"'\"'")
    + "'"
)


class BootstrapClient:
    """Run the fixed pre-gateway bootstrap sequence over OpenSSH and SFTP."""

    def __init__(
        self,
        host: str | Any,
        root: str | PurePosixPath | None = None,
        connect_timeout: float | None = None,
    ) -> None:
        if root is None and not isinstance(host, str):
            config = host
            host = config.host
            root = config.root
            if connect_timeout is None:
                connect_timeout = config.connect_timeout
        if root is None:
            raise TypeError("root is required when host is passed explicitly.")
        self.host = validate_host(host)
        self.root = normalized_cluster_path(root, field="root", safe_token=True)
        timeout = 15.0 if connect_timeout is None else connect_timeout
        if type(timeout) not in {int, float} or not math.isfinite(float(timeout)):
            raise ValueError("connect_timeout must be a positive finite number.")
        self.connect_timeout = float(timeout)
        if not 0 < self.connect_timeout <= 600:
            raise ValueError("connect_timeout must be between 0 and 600 seconds.")

    def _argv(self) -> list[str]:
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={_timeout_option(self.connect_timeout)}",
            "--",
            self.host,
            BOOTSTRAP_REMOTE_COMMAND,
        ]

    def _invoke(
        self,
        stage: str,
        operation_id: str,
        setup: bytes,
        artifact_size: int,
        artifact_digest: str,
    ) -> dict[str, Any]:
        setup_digest = f"sha256:{hashlib.sha256(setup).hexdigest()}"
        identity = b"\n".join(
            (
                BOOTSTRAP_SCHEMA.encode(),
                stage.encode(),
                str(self.root).encode(),
                operation_id.encode(),
                b"request-digest-placeholder",
                str(len(setup)).encode(),
                setup_digest.encode(),
                str(artifact_size).encode(),
                artifact_digest.encode(),
            )
        )
        request_digest = f"sha256:{hashlib.sha256(identity).hexdigest()}"
        envelope = identity.replace(b"request-digest-placeholder", request_digest.encode())
        if stage == "allocate":
            envelope += b"\n" + setup
        else:
            envelope += b"\n"
        try:
            completed = subprocess.run(
                self._argv(),
                input=envelope,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                timeout=self.connect_timeout,
                env=_environment(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GatewayTransportError(
                "ssh-timeout", "The cluster bootstrap timed out.", ambiguous=stage != "probe"
            ) from exc
        except (FileNotFoundError, OSError) as exc:
            raise GatewayTransportError(
                "ssh-unavailable", "OpenSSH is unavailable for cluster bootstrap."
            ) from exc
        if completed.returncode != 0:
            raise GatewayTransportError(
                "bootstrap-prerequisite-missing",
                "The cluster bootstrap prerequisite check failed.",
                ambiguous=stage != "probe",
            )
        if len(completed.stdout) > BOOTSTRAP_MAX_RESPONSE_BYTES:
            raise GatewayTransportError(
                "protocol-incompatible", "The bootstrap response is oversized."
            )
        try:
            value = json.loads(
                completed.stdout,
                object_pairs_hook=_reject_response_duplicates,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError("non-finite bootstrap response")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise GatewayTransportError(
                "protocol-incompatible", "The bootstrap response is invalid."
            ) from exc
        if (
            type(value) is not dict
            or set(value) != {"schema", "status", "upload_path"}
            or value["schema"] != BOOTSTRAP_SCHEMA
            or value["status"]
            not in {"gateway-ready", "bootstrap-required", "upload-authorized"}
            or (
                value["upload_path"] is not None
                and (
                    type(value["upload_path"]) is not str
                    or _SAFE_REMOTE_PATH.fullmatch(value["upload_path"]) is None
                    or not value["upload_path"].startswith(f"{self.root}/temporary/")
                )
            )
        ):
            raise GatewayTransportError(
                "protocol-incompatible", "The bootstrap response is invalid."
            )
        return value

    def probe(self) -> dict[str, Any]:
        return self._invoke(
            "probe",
            str(uuid.uuid4()),
            b"",
            0,
            "sha256:" + "0" * 64,
        )

    def install(self, gateway_artifact: Path, *, setup: bytes = b"true\n") -> None:
        """Install an exact self-contained gateway artifact in two invocations."""
        artifact = Path(gateway_artifact)
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError("gateway_artifact must be a regular non-symlink file.")
        if not setup or b"\x00" in setup or len(setup) > 64 * 1024:
            raise ValueError("setup must be non-empty bounded Bash source without NUL.")
        operation_id = str(uuid.uuid4())
        size = artifact.stat().st_size
        digest = f"sha256:{hashlib.sha256(artifact.read_bytes()).hexdigest()}"
        allocation = self._invoke("allocate", operation_id, setup, size, digest)
        remote_path = allocation["upload_path"]
        if type(remote_path) is not str:
            raise GatewayTransportError(
                "protocol-incompatible", "Bootstrap did not authorize an upload path."
            )
        with tempfile.TemporaryDirectory(prefix="bif-bootstrap-sftp-") as directory:
            local = Path(directory) / "gateway.pyz"
            shutil.copyfile(artifact, local, follow_symlinks=False)
            batch = f'put "{local}" "{remote_path}"\n'.encode("ascii")
            argv = [
                "sftp",
                "-b",
                "-",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={_timeout_option(self.connect_timeout)}",
                "--",
                self.host,
            ]
            try:
                completed = subprocess.run(
                    argv,
                    input=batch,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    timeout=self.connect_timeout,
                    env=_environment(),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise GatewayTransportError(
                    "ssh-timeout", "The bootstrap artifact upload timed out.", ambiguous=True
                ) from exc
            except (FileNotFoundError, OSError) as exc:
                raise GatewayTransportError(
                    "ssh-unavailable", "The SFTP client is unavailable."
                ) from exc
            if completed.returncode != 0:
                raise GatewayTransportError(
                    "ssh-unavailable", "The bootstrap artifact upload failed.", ambiguous=True
                )
        published = self._invoke("publish", operation_id, setup, size, digest)
        if published["status"] != "gateway-ready":
            raise GatewayTransportError(
                "gateway-untrusted", "The gateway was not published."
            )


__all__ = [
    "BOOTSTRAP_MAX_RESPONSE_BYTES",
    "BOOTSTRAP_REMOTE_COMMAND",
    "BOOTSTRAP_SCHEMA",
    "BootstrapClient",
]
