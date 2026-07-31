"""Transport an already prepared immutable cluster bundle."""

from __future__ import annotations

import uuid
from typing import cast

from .cluster_bundle import PreparedClusterBundle
from .types import SSHSubmissionTransport


def submit_prepared_cluster_bundle(
    bundle: PreparedClusterBundle,
    *,
    transport: SSHSubmissionTransport,
    storage_path: str,
) -> str:
    from .ssh import SSHTransportError, _retry_mutation, upload_bundle

    base = {
        "manifest": bundle.manifest,
        "staging_root": str(transport.staging_root),
    }
    allocated = _retry_mutation(
        transport,
        "allocate-upload",
        base,
        str(uuid.uuid4()),
    )
    upload_bundle(transport, bundle, allocated["remote_root"])
    committed = _retry_mutation(
        transport,
        "commit-upload",
        {**base, "upload_id": allocated["upload_id"]},
        str(uuid.uuid4()),
    )
    if (
        committed["upload_id"] != allocated["upload_id"]
        or committed["bundle_digest"] != bundle.digest
    ):
        raise SSHTransportError(
            "remote-protocol",
            "Cluster commit response does not match the allocated bundle.",
            ambiguous=True,
        )
    submitted = _retry_mutation(
        transport,
        "submit",
        {**base, "object_path": committed["object_path"]},
        str(uuid.uuid4()),
    )
    if submitted["storage_path"] != storage_path:
        raise SSHTransportError(
            "remote-protocol",
            "Cluster submit response changed Workflow.storage_path.",
            ambiguous=True,
        )
    return cast(str, submitted["run_id"])
