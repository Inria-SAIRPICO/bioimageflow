from __future__ import annotations

import pytest

from bioimageflow import RunRetryPlan, SSHTransportError
from bioimageflow.launcher.retry_transport import validate_retry_result


def test_retry_plan_response_must_preserve_storage_binding() -> None:
    plan = RunRetryPlan(
        parent_run_id="run_1234567812344abc923456789abcdef0",
        retry_run_id="run_1234567812344abc923456789abcdeff",
        parent_status="failed",
        parent_status_revision=3,
        storage_path="/cluster/other-results",
        retained_submission_digest="sha256:" + "1" * 64,
        retained_material_digest="sha256:" + "2" * 64,
        retained_material_entries=0,
        cache_selection_revision="sha256:" + "3" * 64,
        recompute=None,
        invalidations=(),
        conflicting_run_ids=(),
    )

    with pytest.raises(SSHTransportError, match="parent binding"):
        validate_retry_result(
            "plan-retry",
            plan.to_dict(),
            {
                "run_id": plan.parent_run_id,
                "storage_path": "/cluster/results",
            },
        )
