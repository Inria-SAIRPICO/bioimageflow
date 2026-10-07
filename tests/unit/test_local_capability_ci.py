"""Local capability selection cannot stand in for release CI authority."""

from pathlib import Path
import json
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from tests.unit.test_release_tooling import _job_script, _workflow


ROOT = Path(__file__).parents[2]


def test_local_capability_mode_is_opt_in_and_excludes_unmarked_distributed_trees():
    ci = _workflow(ROOT, "ci.yml")
    flag = ci["on"]["workflow_dispatch"]["inputs"]["local_library_only"]
    assert flag["type"] == "boolean" and flag["default"] == "false"
    floor = ci["on"]["workflow_dispatch"]["inputs"]["core_floor_only"]
    assert floor["type"] == "boolean" and floor["default"] == "false"
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
        "tests/unit/test_distributed_contract.py",
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
            == "github.event_name != 'workflow_dispatch' || (!inputs.local_library_only && !inputs.core_floor_only)"
        )
    assert "if" not in jobs["core-array-lifetime"]


@pytest.mark.parametrize(
    "event,local_only,floor_only,excluded",
    [
        ("push", False, False, {"local-worker-capability"}),
        ("pull_request", False, False, {"local-worker-capability"}),
        ("push", True, True, {"local-worker-capability"}),
        ("pull_request", True, True, {"local-worker-capability"}),
        ("workflow_dispatch", False, False, set()),
        ("workflow_dispatch", True, False, {"parsl-fast-tests", "parsl-process-tests"}),
        ("workflow_dispatch", False, True, None),
        ("workflow_dispatch", True, True, None),
    ],
)
def test_manual_floor_scope_selects_only_existing_matrix(event, local_only, floor_only, excluded):
    jobs = _workflow(ROOT, "ci.yml")["jobs"]
    selected = set()
    for name, job in jobs.items():
        # Evaluate the actual simple Boolean job guards, rather than a copied policy.
        expression = job.get("if", "True").replace("&&", " and ").replace("||", " or ")
        expression = re.sub(r"!(?!=)", " not ", expression).strip()
        if eval(expression, {"__builtins__": {}}, {
            "github": SimpleNamespace(event_name=event),
            "inputs": SimpleNamespace(local_library_only=local_only, core_floor_only=floor_only),
        }):
            selected.add(name)
    expected = (
        {"core-array-lifetime"}
        if event == "workflow_dispatch" and floor_only else set(jobs) - excluded
    )
    assert selected == expected
    core = jobs["core-array-lifetime"]
    assert "needs" not in core
    assert core["strategy"]["matrix"] == {
        "os": ["ubuntu-latest", "windows-latest"], "python": ["3.9", "3.12"],
    }


def test_local_worker_job_proves_explicit_source_capability_separate_from_normal_core():
    jobs = _workflow(ROOT, "ci.yml")["jobs"]
    worker = jobs["local-worker-capability"]
    assert worker["if"] == "github.event_name == 'workflow_dispatch' && !inputs.core_floor_only"
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
