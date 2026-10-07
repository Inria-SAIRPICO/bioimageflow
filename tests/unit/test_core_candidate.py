"""Canonical candidates cannot bypass source, ordinary CI, or artifact identity."""

import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import zipfile

import pytest

from scripts import check_core_candidate as candidate
from tests.unit.test_release_tooling import _workflow


ROOT = Path(__file__).parents[2]
SHA256 = "a" * 64


@pytest.mark.parametrize("event", ["push", "pull_request", "workflow_dispatch"])
def test_native_candidate_defaults_require_no_artifact_authority(event):
    assert candidate.candidate_selection("", "", event, "false") is False


def test_candidate_inputs_admit_only_a_paired_manual_floor_request():
    assert candidate.candidate_selection("123", SHA256, "workflow_dispatch", "true")
    for run, digest, event, floor in [
        ("123", "", "workflow_dispatch", "true"),
        ("", SHA256, "workflow_dispatch", "true"),
        ("123/other", SHA256, "workflow_dispatch", "true"),
        ("123", "not-a-digest", "workflow_dispatch", "true"),
        ("123", SHA256, "push", "true"),
        ("123", SHA256, "pull_request", "true"),
        ("123", SHA256, "workflow_dispatch", "false"),
    ]:
        with pytest.raises(ValueError):
            candidate.candidate_selection(run, digest, event, floor)


@pytest.fixture
def run_document():
    return {
        "id": 123, "repository": {"full_name": "Inria-SAIRPICO/bioimageflow"},
        "path": ".github/workflows/ci.yml", "head_sha": "b" * 40,
        "event": "push", "status": "completed", "conclusion": "success",
    }


def _admit_run(document):
    return candidate.admit_run(
        document, run_id="123", repository="Inria-SAIRPICO/bioimageflow", source_sha="b" * 40,
    )


def test_candidate_requires_the_same_repository_commit_and_successful_ordinary_ci(run_document):
    for event in ("push", "pull_request"):
        assert _admit_run(run_document | {"event": event})["event"] == event
    for update in [
        {"id": 124}, {"repository": {"full_name": "other/project"}},
        {"head_sha": "c" * 40}, {"event": "workflow_dispatch"},
        {"conclusion": "failure"}, {"status": "in_progress"},
        {"path": ".github/workflows/complete.yml"},
    ]:
        with pytest.raises(ValueError):
            _admit_run(run_document | update)


@pytest.fixture(scope="module")
def source():
    sha = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    version, members = candidate.source_identity(ROOT, sha)
    content = {
        name: subprocess.check_output(["git", "-C", str(ROOT), "show", f"HEAD:{candidate.CORE}/{name}"])
        for name in members
    }
    return SimpleNamespace(sha=sha, version=version, members=members, content=content)


def _wheel(directory, source, *, changed=None, version=None):
    path = directory / f"bioimageflow_core-{source.version}-py3-none-any.whl"
    content = source.content | (changed or {})
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in content.items():
            archive.writestr(name, value)
        archive.writestr(
            f"bioimageflow_core-{source.version}.dist-info/METADATA",
            f"Metadata-Version: 2.3\nName: bioimageflow-core\nVersion: {version or source.version}\n",
        )
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_admitted_canonical_wheel_contains_exact_git_python_and_typing_bytes(tmp_path, source):
    wheel, digest = _wheel(tmp_path, source)
    path, identity = candidate.admit_wheel(
        tmp_path, version=source.version, digest=digest, members=source.members,
    )
    assert path == wheel.resolve()
    assert identity["wheel_sha256"] == digest
    assert identity["module_hashes"] == source.members
    assert "bioimageflow_core/py.typed" in identity["module_hashes"]
    with pytest.raises(ValueError, match="commit differs"):
        candidate.source_identity(ROOT, "0" * 40)


@pytest.mark.parametrize("fault", ["held-hash", "source-body", "extra-member", "metadata"])
def test_candidate_refuses_wrong_hash_source_inventory_or_distribution_before_install(tmp_path, source, fault):
    changed = {
        "source-body": {"bioimageflow_core/__init__.py": b"foreign body\n"},
        "extra-member": {"bioimageflow_core/unadmitted.py": b"pass\n"},
    }.get(fault)
    _path, digest = _wheel(tmp_path, source, changed=changed, version="9.0" if fault == "metadata" else None)
    if fault == "held-hash":
        digest = SHA256
    with pytest.raises(ValueError):
        candidate.admit_wheel(tmp_path, version=source.version, digest=digest, members=source.members)


def test_candidate_requires_exactly_one_current_core_wheel(tmp_path, source):
    wheel, digest = _wheel(tmp_path, source)
    (tmp_path / "bioimageflow_core-9.0-py3-none-any.whl").write_bytes(wheel.read_bytes())
    with pytest.raises(ValueError, match="exactly"):
        candidate.admit_wheel(tmp_path, version=source.version, digest=digest, members=source.members)


def test_candidate_cli_reports_only_admitted_run_and_verified_wheel(tmp_path, source, run_document, monkeypatch):
    document = run_document | {"head_sha": source.sha}
    real = candidate.subprocess.check_output

    def command(arguments):
        if arguments[0] == "gh":
            assert arguments == ["gh", "api", "repos/Inria-SAIRPICO/bioimageflow/actions/runs/123"]
            return json.dumps(document).encode()
        return real(arguments)

    monkeypatch.setattr(candidate.subprocess, "check_output", command)
    wheel, digest = _wheel(tmp_path, source)
    run_receipt, final_receipt, output = [tmp_path / name for name in ("run.json", "wheel.json", "output")]
    assert candidate.main([
        "run", "--run-id", "123", "--sha256", digest, "--repository", "Inria-SAIRPICO/bioimageflow",
        "--source-sha", source.sha, "--source-root", str(ROOT), "--receipt", str(run_receipt),
    ]) == 0
    assert candidate.main([
        "wheel", "--artifacts", str(tmp_path), "--run-receipt", str(run_receipt),
        "--source-root", str(ROOT), "--receipt", str(final_receipt), "--github-output", str(output),
    ]) == 0
    held = json.loads(final_receipt.read_text())
    assert held["run"]["head_sha"] == source.sha
    assert held["run"]["id"] == 123
    assert held["artifact"]["module_hashes"] == source.members
    assert output.read_text() == f"wheel={wheel.resolve()}\n"


def test_workflow_verifies_candidate_before_install_and_keeps_native_build_defaults():
    ci = _workflow(ROOT, "ci.yml")
    inputs = ci["on"]["workflow_dispatch"]["inputs"]
    assert inputs["candidate_run_id"]["default"] == ""
    assert inputs["candidate_core_sha256"]["default"] == ""
    job = ci["jobs"]["core-array-lifetime"]
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    steps = job["steps"]
    names = [step["name"] for step in steps]
    assert names.index("Admit exact successful ordinary candidate CI run") < names.index("Download the admitted ordinary packages artifact")
    assert names.index("Verify canonical Core bytes before installation") < names.index("Install normal isolated artifact closure")
    for selected in (False, True):
        outputs = SimpleNamespace(candidate="true" if selected else "false")
        context = {"steps": SimpleNamespace(core_candidate=SimpleNamespace(outputs=outputs))}
        for step in steps:
            if "core_candidate.outputs.candidate" in step.get("if", ""):
                enabled = eval(step["if"], {"__builtins__": {}}, context)
                assert enabled == (not selected if step["name"].startswith("Build current") else selected)
    download = next(step for step in steps if step["name"].startswith("Download the admitted"))
    assert download["with"]["name"] == "packages"
    assert download["with"]["run-id"] == "${{ inputs.candidate_run_id }}"
    for step in steps:
        assert "${{ inputs." not in step.get("run", "")
    execute = next(step["run"] for step in steps if step["name"].startswith("Check source-disabled"))
    assert '--wheel "$CORE_ARRAY_WHEEL"' in execute and "-I check_core_array_lifetime.py" in execute
