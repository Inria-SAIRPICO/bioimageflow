"""External output-root export integration tests."""

import shutil
import stat
from pathlib import Path

import pytest

from bioimageflow import Workflow, export_outputs
from bioimageflow.storage import (
    Storage,
    asset_digest_and_size,
    make_result_key,
)
from bioimageflow.storage import exports as export_module

from tests.testkit.runtime_cache import SourceAssetWriter, _run_dirs
from tests.testkit.storage import _write_record


def _computed_output(
    tmp_path: Path,
    *,
    text: str = "exported",
    output_view: str | None = None,
) -> tuple[Path, str, str]:
    storage_path = tmp_path / "results"
    with Workflow(
        engine="direct",
        storage_path=storage_path,
        output_view=output_view,
    ) as workflow:
        node = SourceAssetWriter()(text=text)
        workflow.compute(node)
    [run_dir] = _run_dirs(storage_path)
    return storage_path, node.name, run_dir.name


def test_workflow_exports_independent_latest_copy_to_destination(
    tmp_path: Path,
) -> None:
    storage_path, node_name, _ = _computed_output(
        tmp_path,
        text="canonical",
        output_view="symlink",
    )
    published = storage_path / "outputs" / "latest" / node_name / "mask_0.txt"
    assert published.is_symlink()
    published.unlink()
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("not canonical")
    published.symlink_to(unrelated)

    destination = tmp_path / "shared-results"
    with Workflow(engine="direct", storage_path=storage_path) as workflow:
        materialized = workflow.export_outputs(
            destination=destination,
            mode="copy",
            scope="latest",
        )

    output = destination / "latest" / node_name / "mask_0.txt"
    assert materialized == [
        output,
        output.parent / "dataframe.parquet",
        output.parent / "dataframe.csv",
        output.parent / "dataframe.json",
        output.parent / "provenance.json",
    ]
    assert output.read_text() == "canonical"
    assert not output.is_symlink()
    shutil.rmtree(storage_path)
    unrelated.unlink()
    assert output.read_text() == "canonical"


def test_destination_both_scope_preserves_latest_and_run_layouts(
    tmp_path: Path,
) -> None:
    storage_path, node_name, run_id = _computed_output(tmp_path)
    destination = tmp_path / "complete-results"

    materialized = export_outputs(
        storage_path,
        destination=destination,
        mode="copy",
        scope="both",
        run_id=run_id,
    )

    latest = destination / "latest" / node_name / "mask_0.txt"
    run = (
        destination
        / "runs"
        / run_id
        / "nodes"
        / node_name
        / "outputs"
        / "assets"
        / "mask_0.txt"
    )
    assert latest.read_text() == "exported"
    assert run.read_text() == "exported"
    assert latest in materialized
    assert run in materialized
    assert all(destination in path.parents for path in materialized)


def test_destination_runs_scope_selects_latest_success(
    tmp_path: Path,
) -> None:
    storage_path, node_name, run_id = _computed_output(tmp_path)
    destination = tmp_path / "run-results"

    materialized = export_outputs(
        storage_path,
        destination=destination,
        mode="copy",
        scope="runs",
    )

    output = (
        destination
        / "runs"
        / run_id
        / "nodes"
        / node_name
        / "outputs"
        / "assets"
        / "mask_0.txt"
    )
    assert output in materialized
    assert output.read_text() == "exported"
    assert not (destination / "latest").exists()


def test_destination_copy_preserves_directory_after_source_removal(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    storage = Storage(storage_path)
    identity_directory = tmp_path / "identity"
    identity_directory.mkdir()
    (identity_directory / "content.txt").write_bytes(b"mask")
    size, digest = asset_digest_and_size(identity_directory)
    output = {
        "path": "assets/dataset.zarr",
        "kind": "owned_asset",
        "asset_type": "directory",
        "size": size,
        "digest": digest,
    }
    result_key = make_result_key({"node": "directory-export"})
    record_id = _write_record(storage, result_key, outputs=[output])
    run_id = "run_directory"
    node_name = "Directory_1"
    storage.write_run_metadata(
        run_id,
        workflow_identity="workflow-directory",
        engine="direct:parallel",
        status="succeeded",
        target_nodes=[node_name],
    )
    storage.write_run_node_result(
        run_id,
        node_name,
        result_key=result_key,
        record_id=record_id,
        cache_hit=False,
    )
    storage.update_latest_node(node_name, run_id)
    destination = tmp_path / "directory-results"

    materialized = export_outputs(
        storage_path,
        destination=destination,
        mode="copy",
        scope="latest",
    )

    directory = destination / "latest" / node_name / "dataset.zarr"
    assert materialized == [
        directory,
        directory.parent / "dataframe.parquet",
        directory.parent / "dataframe.csv",
        directory.parent / "dataframe.json",
        directory.parent / "provenance.json",
    ]
    shutil.rmtree(storage_path)
    assert (directory / "content.txt").read_bytes() == b"mask"


def test_destination_requires_replace_and_replaces_the_whole_tree(
    tmp_path: Path,
) -> None:
    storage_path, node_name, _ = _computed_output(tmp_path)
    destination = tmp_path / "shared-results"
    destination.mkdir()
    stale = destination / "stale.txt"
    stale.write_text("keep unless replaced")

    with pytest.raises(FileExistsError, match="already exists"):
        export_outputs(storage_path, destination=destination)
    assert stale.read_text() == "keep unless replaced"

    materialized = export_outputs(
        storage_path,
        destination=destination,
        replace=True,
    )

    output = destination / "latest" / node_name / "mask_0.txt"
    assert output in materialized
    assert output.read_text() == "exported"
    assert not stale.exists()


def test_destination_replacement_rolls_back_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_path, _, _ = _computed_output(tmp_path)
    destination = tmp_path / "shared-results"
    destination.mkdir()
    marker = destination / "previous.txt"
    marker.write_text("previous")
    real_publish = export_module.publish_no_replace

    def fail_install(source: str | Path, target: str | Path) -> None:
        source_path = Path(source)
        if Path(target) == destination and source_path.name.endswith(".tmp"):
            raise OSError("simulated export installation failure")
        real_publish(Path(source), Path(target))

    monkeypatch.setattr(export_module, "publish_no_replace", fail_install)
    with pytest.raises(OSError, match="simulated export installation failure"):
        export_outputs(
            storage_path,
            destination=destination,
            replace=True,
        )

    assert marker.read_text() == "previous"
    assert not list(tmp_path.glob(".shared-results.*"))


def test_destination_materialization_failure_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_path, _, _ = _computed_output(tmp_path)
    destination = tmp_path / "shared-results"

    def fail_materialization(self: Storage, mode: str) -> list[Path]:
        raise OSError("simulated export materialization failure")

    monkeypatch.setattr(Storage, "materialize_latest_outputs", fail_materialization)
    with pytest.raises(OSError, match="simulated export materialization failure"):
        export_outputs(storage_path, destination=destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".shared-results.*"))


def test_destination_rejects_source_storage_overlap(tmp_path: Path) -> None:
    storage_path, _, _ = _computed_output(tmp_path)
    destinations = (
        storage_path,
        storage_path / "shared-results",
        storage_path.parent,
    )

    for destination in destinations:
        with pytest.raises(ValueError, match="source storage root"):
            export_outputs(storage_path, destination=destination)


def test_replace_requires_explicit_destination(tmp_path: Path) -> None:
    storage_path, _, _ = _computed_output(tmp_path)

    with pytest.raises(ValueError, match="requires an explicit"):
        export_outputs(storage_path, replace=True)


def test_export_no_replace_preserves_late_winner(tmp_path, monkeypatch):
    storage, _, _ = _computed_output(tmp_path)
    target = tmp_path / 'late-export'
    original = Storage.materialize_latest_outputs
    winner = {}

    def materialize(self, mode):
        paths = original(self, mode)
        target.mkdir()
        (target / 'sentinel').write_text('foreign winner')
        winner['inode'] = target.stat().st_ino
        return paths

    monkeypatch.setattr(Storage, 'materialize_latest_outputs', materialize)
    error = None
    try:
        export_outputs(storage, destination=target, replace=False)
    except FileExistsError as exc:
        error = exc
    observed = {'error': str(error), 'inode': target.stat().st_ino,
                'sentinel': (target / 'sentinel').read_text() if (target / 'sentinel').exists() else None}
    assert error is not None, observed
    assert observed['inode'] == winner['inode'], observed
    assert observed['sentinel'] == 'foreign winner', observed


@pytest.mark.parametrize("initially_present", [False, True], ids=["absent-then-created", "present-then-substituted"])
def test_export_replace_preserves_materialization_late_owner(tmp_path, monkeypatch, initially_present):
    storage, _, _ = _computed_output(tmp_path)
    target = tmp_path / "late-replacement"
    displaced = tmp_path / "admitted-original"
    original_inode = None
    if initially_present:
        target.mkdir()
        (target / "original.txt").write_text("original admitted target")
        original_inode = target.stat().st_ino
    materialize = Storage.materialize_latest_outputs
    winner = {}

    def materialize_then_replace_owner(self, mode):
        paths = materialize(self, mode)
        if initially_present:
            target.rename(displaced)
        target.mkdir()
        (target / "sentinel").write_text("unrelated late winner")
        winner["inode"] = target.stat().st_ino
        return paths

    monkeypatch.setattr(Storage, "materialize_latest_outputs", materialize_then_replace_owner)
    with pytest.raises(FileExistsError):
        export_outputs(storage, destination=target, mode="copy", replace=True)

    assert target.stat().st_ino == winner["inode"]
    assert (target / "sentinel").read_text() == "unrelated late winner"
    assert list(target.iterdir()) == [target / "sentinel"]
    if initially_present:
        assert displaced.stat().st_ino == original_inode
        assert (displaced / "original.txt").read_text() == "original admitted target"
    assert not list(tmp_path.glob(".late-replacement.*"))


def test_export_replace_late_installation_owner_keeps_recoverable_original(tmp_path, monkeypatch, caplog):
    storage, _, _ = _computed_output(tmp_path)
    target = tmp_path / "late-installation"
    target.mkdir()
    (target / "original.txt").write_text("admitted original")
    original_inode = target.stat().st_ino
    publish = export_module.publish_no_replace
    winner = {}

    def publish_after_late_owner(source, destination):
        if Path(destination) == target and Path(source).name.endswith(".tmp"):
            backups = list(tmp_path.glob(".late-installation.*.backup"))
            assert len(backups) == 1 and backups[0].stat().st_ino == original_inode
            assert not target.exists(), "the admitted original must already be backed up"
            target.mkdir()
            (target / "sentinel").write_text("installation winner")
            winner["inode"] = target.stat().st_ino
        try:
            publish(Path(source), Path(destination))
        except FileExistsError as error:
            if Path(source).name.endswith(".tmp"):
                winner["failure"] = error
            raise

    monkeypatch.setattr(export_module, "publish_no_replace", publish_after_late_owner)
    with pytest.raises(FileExistsError) as failure:
        export_outputs(storage, destination=target, mode="copy", replace=True)

    assert failure.value is winner["failure"]
    assert target.stat().st_ino == winner["inode"]
    assert (target / "sentinel").read_text() == "installation winner"
    assert list(target.iterdir()) == [target / "sentinel"]
    [backup] = tmp_path.glob(".late-installation.*.backup")
    assert backup.stat().st_ino == original_inode
    assert (backup / "original.txt").read_text() == "admitted original"
    diagnostic = " ".join([str(failure.value), *getattr(failure.value, "__notes__", ()), caplog.text])
    assert str(backup) in diagnostic, "the preserved original must have a reported recovery address"
    assert not list(tmp_path.glob(".late-installation.*.tmp"))


def test_export_installed_tree_survives_pending_backup_cleanup(tmp_path, monkeypatch, caplog):
    storage, node_name, _ = _computed_output(tmp_path)
    target = tmp_path / "pending-cleanup"
    target.mkdir()
    (target / "original.txt").write_text("original before cleanup")
    original = target.lstat()
    remove = export_module._remove_path
    cleanup_error = OSError("controlled failure before superseded backup deletion")
    attempted = []

    def fail_backup_cleanup(path):
        if Path(path).name.endswith(".backup"):
            attempted.append(Path(path))
            raise cleanup_error
        remove(path)

    monkeypatch.setattr(export_module, "_remove_path", fail_backup_cleanup)
    with pytest.raises(OSError) as failure:
        export_outputs(storage, destination=target, mode="copy", replace=True)

    assert failure.value is cleanup_error
    assert target.stat().st_ino != original.st_ino
    assert (target / "latest" / node_name / "mask_0.txt").read_text() == "exported"
    assert not (target / "original.txt").exists()
    [backup] = attempted
    assert list(tmp_path.glob(".pending-cleanup.*.backup")) == [backup]
    assert backup.stat().st_ino == original.st_ino
    assert (backup / "original.txt").read_text() == "original before cleanup"
    assert failure.value.export_cleanup == {
        "installed_destination": str(target),
        "pending_backup": {"path": str(backup),
                           "identity": (original.st_dev, original.st_ino, stat.S_IFMT(original.st_mode))},
    }
    diagnostic = " ".join([str(failure.value), *getattr(failure.value, "__notes__", ()), caplog.text])
    assert str(target) in diagnostic and str(backup) in diagnostic
    assert not list(tmp_path.glob(".pending-cleanup.*.tmp"))
