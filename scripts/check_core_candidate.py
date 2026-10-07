"""Admit a canonical Core wheel from ordinary CI before a manual floor run."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import importlib
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
import zipfile


# CI runs the helper on 3.11; developer checks use the declared 3.10 TOML dependency.
tomllib = importlib.import_module("tomllib" if sys.version_info >= (3, 11) else "tomli")


CORE = "packages/bioimageflow-core"


def candidate_selection(run_id: str, digest: str, event: str, floor_only: str) -> bool:
    if bool(run_id) != bool(digest):
        raise ValueError("Candidate run ID and Core SHA256 must be supplied together")
    if not run_id:
        return False
    if event != "workflow_dispatch" or floor_only != "true":
        raise ValueError("Canonical candidates require a manual floor-only run")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", run_id):
        raise ValueError("Candidate run ID must be a positive decimal integer")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Candidate Core SHA256 must be 64 lowercase hexadecimal characters")
    return True


def _git(root: Path, *arguments: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *arguments])


def source_identity(root: Path, source_sha: str) -> tuple[str, dict[str, str]]:
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("Expected source must be an exact commit SHA")
    if _git(root, "rev-parse", "HEAD").decode().strip() != source_sha:
        raise ValueError("Checked-out commit differs from the candidate source authority")
    project = tomllib.loads(_git(root, "show", f"HEAD:{CORE}/pyproject.toml").decode())
    version = project["project"]["version"]
    if project["project"]["name"] != "bioimageflow-core" or not isinstance(version, str):
        raise ValueError("Current Core project identity is invalid")
    paths = _git(root, "ls-tree", "-r", "--name-only", "HEAD", "--", f"{CORE}/bioimageflow_core").decode().splitlines()
    members = {
        path.removeprefix(f"{CORE}/"): hashlib.sha256(_git(root, "show", f"HEAD:{path}")).hexdigest()
        for path in paths if path.endswith(".py") or path.endswith("/py.typed")
    }
    if "bioimageflow_core/__init__.py" not in members or "bioimageflow_core/py.typed" not in members:
        raise ValueError("Current Core source inventory is incomplete")
    return version, members


def admit_run(document: dict, *, run_id: str, repository: str, source_sha: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected repository identity is invalid")
    if type(document.get("id")) is not int or document["id"] != int(run_id):
        raise ValueError("Candidate run identity differs from the requested run")
    if document.get("repository", {}).get("full_name") != repository:
        raise ValueError("Candidate run belongs to another repository")
    if document.get("path") != ".github/workflows/ci.yml":
        raise ValueError("Candidate artifact must come from the ordinary CI workflow")
    if document.get("event") not in {"push", "pull_request"}:
        raise ValueError("Manual capability runs cannot supply ordinary CI candidate authority")
    if document.get("status") != "completed" or document.get("conclusion") != "success":
        raise ValueError("Candidate ordinary CI must have completed successfully")
    if document.get("head_sha") != source_sha:
        raise ValueError("Candidate ordinary CI must name the exact current commit")
    return {key: document[key] for key in ("id", "path", "event", "status", "conclusion", "head_sha")} | {"repository": repository}


def admit_wheel(artifacts: Path, *, version: str, digest: str, members: dict[str, str]) -> tuple[Path, dict]:
    candidates = [path for path in artifacts.glob("*.whl") if path.name.lower().startswith("bioimageflow_core-")]
    expected_name = f"bioimageflow_core-{version}-py3-none-any.whl"
    if len(candidates) != 1 or candidates[0].name != expected_name:
        raise ValueError("Expected exactly the current normal Core wheel filename")
    wheel = candidates[0]
    if not stat.S_ISREG(wheel.lstat().st_mode):
        raise ValueError("Candidate Core wheel must be a regular file")
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != digest:
        raise ValueError("Candidate Core wheel differs from the held SHA256")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Candidate wheel contains duplicate archive members")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError("Candidate wheel must have exactly one distribution METADATA")
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        if metadata["Name"] != "bioimageflow-core" or metadata["Version"] != version:
            raise ValueError("Candidate wheel distribution identity differs from current Core")
        actual = {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in names if name.startswith("bioimageflow_core/") and (name.endswith(".py") or name.endswith("/py.typed"))
        }
    if actual != members:
        raise ValueError("Candidate Core members differ from the complete current Git source inventory")
    return wheel.resolve(), {"version": version, "wheel_sha256": digest, "module_hashes": actual}


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select")
    select.add_argument("--run-id", default="")
    select.add_argument("--sha256", default="")
    select.add_argument("--event", required=True)
    select.add_argument("--floor-only", default="false")
    select.add_argument("--github-output", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--run-id", required=True)
    run.add_argument("--sha256", required=True)
    run.add_argument("--repository", required=True)
    run.add_argument("--source-sha", required=True)
    run.add_argument("--source-root", type=Path, required=True)
    run.add_argument("--receipt", type=Path, required=True)
    wheel = commands.add_parser("wheel")
    wheel.add_argument("--artifacts", type=Path, required=True)
    wheel.add_argument("--run-receipt", type=Path, required=True)
    wheel.add_argument("--source-root", type=Path, required=True)
    wheel.add_argument("--receipt", type=Path, required=True)
    wheel.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "select":
            selected = candidate_selection(args.run_id, args.sha256, args.event, args.floor_only)
            with args.github_output.open("a") as output:
                output.write(f"candidate={'true' if selected else 'false'}\n")
        elif args.command == "run":
            candidate_selection(args.run_id, args.sha256, "workflow_dispatch", "true")
            version, members = source_identity(args.source_root, args.source_sha)
            raw = subprocess.check_output(["gh", "api", f"repos/{args.repository}/actions/runs/{args.run_id}"])
            authority = admit_run(json.loads(raw), run_id=args.run_id, repository=args.repository, source_sha=args.source_sha)
            _write(args.receipt, {"run": authority, "held_sha256": args.sha256, "version": version, "source_members": members,
                                  "artifact_name": "packages", "release_tag_if_published": f"bioimageflow-core-v{version}"})
        else:
            held = json.loads(args.run_receipt.read_text())
            authority = held["run"]
            version, members = source_identity(args.source_root, authority["head_sha"])
            if version != held["version"] or members != held["source_members"]:
                raise ValueError("Candidate source identity changed after run admission")
            path, identity = admit_wheel(args.artifacts, version=version, digest=held["held_sha256"], members=members)
            _write(args.receipt, held | {"wheel": str(path), "artifact": identity,
                                       "qualification": "Canonical Git bytes, including LF Python on Windows; no tag existence or publisher eligibility claim"})
            if "\n" in str(path) or "\r" in str(path):
                raise ValueError("Candidate wheel path is invalid for workflow output")
            with args.github_output.open("a") as output:
                output.write(f"wheel={path}\n")
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Core candidate refused: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
