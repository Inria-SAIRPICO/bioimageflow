"""Local capability selection cannot stand in for release CI authority."""

from pathlib import Path
import json
import re
import shlex
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from tests.unit.test_release_tooling import _job_script, _workflow


ROOT = Path(__file__).parents[2]
UNIT_DISTRIBUTED_PATHS = {
    "tests/unit/parsl", "tests/unit/cluster", "tests/unit/launcher",
    "tests/unit/test_distributed_contract.py",
}
INTEGRATION_DISTRIBUTED_PATHS = {"tests/integration/parsl", "tests/integration/launcher"}


def test_ordinary_local_ci_excludes_exact_distributed_paths_and_preserves_shared_controls():
    ci = _workflow(ROOT, "ci.yml")
    assert "local_library_only" not in ci["on"]["workflow_dispatch"]["inputs"]
    floor = ci["on"]["workflow_dispatch"]["inputs"]["core_floor_only"]
    assert floor["type"] == "boolean" and floor["default"] == "false"
    selection = shlex.split(ci["env"]["LOCAL_LIBRARY_PYTEST_ARGS"])
    assert set(selection) == {
        "--ignore=" + path for path in UNIT_DISTRIBUTED_PATHS | INTEGRATION_DISTRIBUTED_PATHS
    }
    assert len(selection) == 6
    jobs = ci["jobs"]
    for name in [
        "unit-tests",
        "integration-tests",
        "compatibility-tests",
        "deterministic-tests",
    ]:
        assert "$LOCAL_LIBRARY_PYTEST_ARGS" in _job_script(jobs[name])
    assert "parsl-fast-tests" not in jobs and "parsl-process-tests" not in jobs
    assert "if" not in jobs["core-array-lifetime"]


@pytest.mark.parametrize(
    "event,floor_only,excluded",
    [
        ("push", False, {"local-worker-capability"}),
        ("pull_request", False, {"local-worker-capability"}),
        ("push", True, {"local-worker-capability"}),
        ("pull_request", True, {"local-worker-capability"}),
        ("workflow_dispatch", False, set()),
        ("workflow_dispatch", True, None),
    ],
)
def test_manual_floor_scope_selects_only_existing_matrix(event, floor_only, excluded):
    jobs = _workflow(ROOT, "ci.yml")["jobs"]
    selected = set()
    for name, job in jobs.items():
        # Evaluate the actual simple Boolean job guards, rather than a copied policy.
        expression = job.get("if", "True").replace("&&", " and ").replace("||", " or ")
        expression = re.sub(r"!(?!=)", " not ", expression).strip()
        if eval(expression, {"__builtins__": {}}, {
            "github": SimpleNamespace(event_name=event),
            "inputs": SimpleNamespace(core_floor_only=floor_only),
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


def test_distributed_ci_retains_complementary_unmarked_and_marked_coverage_without_suppression():
    distributed = _workflow(ROOT, "distributed.yml")
    assert distributed["on"] == {"push": {"branches": ["main"]}, "pull_request": "", "workflow_dispatch": ""}
    assert distributed["concurrency"]["group"] != _workflow(ROOT, "ci.yml")["concurrency"]["group"]
    jobs = distributed["jobs"]
    assert set(jobs) == {
        "distributed-unit-tests", "distributed-integration-tests", "parsl-fast-tests", "parsl-process-tests",
    }
    for name, expected in [
        ("distributed-unit-tests", UNIT_DISTRIBUTED_PATHS),
        ("distributed-integration-tests", INTEGRATION_DISTRIBUTED_PATHS),
    ]:
        script = _job_script(jobs[name])
        command = shlex.split(next(line for line in script.splitlines() if "pytest " in line))
        assert set(command[3:command.index("-m")]) == expected
        assert command[command.index("-m") + 1] == (
            "not slow and not acceptance and not packaging and not package_tools and not complete "
            "and not wetlands and not public_data and not external_binary and not sairpico_binary "
            "and not model_runtime and not parsl"
        )
        assert jobs[name]["strategy"]["matrix"]["python"] == ["3.10", "3.12"]
    assert 'uv run pytest tests -m "parsl and not slow"' in _job_script(jobs["parsl-fast-tests"])
    assert 'uv run pytest tests -m "parsl and slow"' in _job_script(jobs["parsl-process-tests"])
    assert jobs["parsl-fast-tests"]["strategy"]["matrix"]["python"] == ["3.10", "3.12"]
    assert jobs["parsl-process-tests"]["env"]["UV_PYTHON"] == "3.11"
    for job in jobs.values():
        assert "if" not in job and "continue-on-error" not in job
        assert "|| true" not in _job_script(job)


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
