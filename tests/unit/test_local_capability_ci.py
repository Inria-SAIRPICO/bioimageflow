"""Local capability selection cannot stand in for release CI authority."""

from pathlib import Path
import json
import re
import shutil
import subprocess

import pytest

from tests.unit.test_release_tooling import _job_script, _workflow


ROOT = Path(__file__).parents[2]


def test_local_capability_mode_is_opt_in_and_excludes_unmarked_distributed_trees():
    ci = _workflow(ROOT, "ci.yml")
    flag = ci["on"]["workflow_dispatch"]["inputs"]["local_library_only"]
    assert flag["type"] == "boolean" and flag["default"] == "false"
    selection = ci["env"]["LOCAL_LIBRARY_PYTEST_ARGS"]
    assert (
        "github.event_name == 'workflow_dispatch' && inputs.local_library_only"
        in selection
    )
    assert "|| ''" in selection
    for tree in [
        "tests/unit/parsl",
        "tests/unit/cluster",
        "tests/unit/launcher",
        "tests/integration/parsl",
        "tests/integration/launcher",
    ]:
        assert f"--ignore={tree}" in selection
    jobs = ci["jobs"]
    for name in [
        "unit-tests",
        "integration-tests",
        "compatibility-tests",
        "deterministic-tests",
    ]:
        assert "$LOCAL_LIBRARY_PYTEST_ARGS" in _job_script(jobs[name])
    for name in ["parsl-fast-tests", "parsl-process-tests"]:
        assert (
            jobs[name]["if"]
            == "github.event_name != 'workflow_dispatch' || !inputs.local_library_only"
        )
    for name in ["quality", "packages", "docs", "core-array-lifetime"]:
        assert "if" not in jobs[name]


def test_local_worker_job_proves_explicit_source_capability_separate_from_normal_core():
    jobs = _workflow(ROOT, "ci.yml")["jobs"]
    worker = jobs["local-worker-capability"]
    assert worker["if"] == "github.event_name == 'workflow_dispatch'"
    assert worker["env"]["UV_PYTHON"] == "3.10"
    assert worker["env"]["BIOIMAGEFLOW_CORE_SOURCE"].endswith(
        "/packages/bioimageflow-core"
    )
    script = _job_script(worker)
    assert "--run-complete" in script
    assert script.count(".py") == 4
    for path in [
        "tests/integration/test_wetlands_smoke.py",
        "tests/integration/test_wetlands_task_api.py",
        "tests/integration/test_wetlands_cancellation.py",
        "packages/bioimageflow-common-tools/tests/test_common_complete_wetlands.py",
    ]:
        assert path in script
    installed = _job_script(jobs["core-array-lifetime"])
    assert (
        "--no-sources" in installed and "-I check_core_array_lifetime.py" in installed
    )


def test_manual_capability_success_cannot_satisfy_exact_release_commit_gate():
    script = _job_script(_workflow(ROOT, "release.yml")["jobs"]["prepare"])
    assert 'head_sha="$RELEASE_SHA"' in script
    assert (
        'select(.conclusion == "success" and (.event == "push" or .event == "pull_request"))'
        in script
    )
    assert "workflow_dispatch" not in script
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("actual release predicate control requires jq")
    query = re.search(r"--jq '([^']+)'", script).group(1)
    samples = [
        {"event": "workflow_dispatch", "conclusion": "success"},
        {"event": "push", "conclusion": "failure"},
        {"event": "push", "conclusion": "success"},
        {"event": "pull_request", "conclusion": "success"},
    ]
    result = subprocess.run(
        [jq, query], input="\n".join(json.dumps({"workflow_runs": [run]}) for run in samples),
        text=True, capture_output=True, check=True,
    )
    assert result.stdout.splitlines() == ["0", "0", "1", "1"]
